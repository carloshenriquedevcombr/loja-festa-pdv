"""Funcionários: cadastrar, cargos (fixo ou freelance com prazo), desativar, redefinir senha.
E "Minha senha" para qualquer pessoa trocar a própria."""
import re
from datetime import date
from urllib.parse import quote
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import cripto, db, pessoas, seguranca
from ..auth import auditar, nova_sessao, pode, requer, usuario_atual
from ..pagamento import formulario
from ..web import checar_csrf, mensagem_do_banco, tela

router = APIRouter()
GERIR = ("usuarios.operacionais", "rh.funcionarios")
DIRETORIA = ("DONO", "RH", "FINANCEIRO", "MASTER")


def _papeis_disponiveis(cur, usuario) -> list[dict]:
    """Quem é Dono vê todos os cargos; os demais, só os operacionais."""
    cur.execute("SELECT codigo, nome, diretoria FROM papeis ORDER BY diretoria, id")
    return [p for p in cur.fetchall()
            if pode(usuario, "usuarios.diretoria") or (not p["diretoria"] and p["codigo"] != "DESENVOLVEDOR")]


def _pessoa(cur, uid):
    cur.execute("""
        SELECT u.id::text, u.nome, u.login, u.ativo, u.vinculo, u.telefone_c, u.data_admissao, u.observacoes_c,
               u.avisos,
               (u.bloqueado_ate > now()) AS bloqueado,
               COALESCE(array_agg(p.codigo) FILTER (WHERE up.valido_ate IS NULL OR up.valido_ate > now()), '{}') AS ativos,
               max(up.valido_ate) FILTER (WHERE up.valido_ate > now()) AS ate
          FROM usuarios u LEFT JOIN usuario_papeis up ON up.usuario_id = u.id
          LEFT JOIN papeis p ON p.id = up.papel_id
         WHERE u.id = %s AND NOT u.sistema GROUP BY u.id""", (uid,))
    return pessoas.abrir_usuario(cur.fetchone())


def _pode_mexer(usuario, pessoa) -> str | None:
    if pessoa["id"] == usuario["id"]:
        return "Para mudar os seus próprios dados de acesso, peça a outra pessoa da Diretoria."
    if set(pessoa["ativos"]) & set(DIRETORIA) and not pode(usuario, "usuarios.diretoria"):
        return "Só a Dona pode alterar pessoas da Diretoria."
    return None


def _senha_nova(form, *pessoais) -> tuple[str | None, str | None]:
    senha = form.get("senha") or ""
    if senha != (form.get("senha2") or ""):
        return None, "As duas senhas não conferem."
    erro = seguranca.validar_senha_nova(senha, (form.get("login"), form.get("nome"), *pessoais))
    return (None, erro) if erro else (senha, None)


AVISOS = {"pedidos": "Pedido novo na loja", "site": "Pedido de prova pelo site", "chat": "Chat do site",
          "pagamentos": "Pagamento pelo link", "agenda": "Agenda do dia (8h)", "costura": "Peças para ajustar",
          "limpeza": "Peças para limpar", "relatorio": "Resumo do caixa (fim do dia)", "seguranca": "Alertas de acesso"}
PADRAO = {"DONO": ["pedidos", "site", "pagamentos", "relatorio", "seguranca"], "DESENVOLVEDOR": ["seguranca"],
          "GERENTE": ["pedidos", "site", "pagamentos", "agenda", "chat"], "ATENDENTE": ["site", "agenda", "chat"],
          "COSTUREIRA": ["costura"], "AJUDANTE_COSTURA": ["costura"], "LIMPEZA": ["limpeza"],
          "FINANCEIRO": ["pagamentos", "relatorio"]}


def avisos_padrao(cargos) -> list[str]:
    return sorted({a for c in cargos for a in PADRAO.get(c, [])})


def _data(texto) -> date | None:
    try:
        return date.fromisoformat(texto) if texto else None
    except ValueError:
        return None


@router.get("/equipe")
def lista(request: Request, usuario: dict = Depends(requer(*GERIR))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""
            SELECT u.id::text, u.nome, u.login, u.ativo, u.vinculo, (u.bloqueado_ate > now()) AS bloqueado,
                   COALESCE(string_agg(p.nome, ', ' ORDER BY p.id)
                            FILTER (WHERE up.valido_ate IS NULL OR up.valido_ate > now()), '—') AS cargos,
                   max(up.valido_ate) FILTER (WHERE up.valido_ate > now()) AS ate
              FROM usuarios u LEFT JOIN usuario_papeis up ON up.usuario_id = u.id
              LEFT JOIN papeis p ON p.id = up.papel_id
             WHERE NOT u.sistema
             GROUP BY u.id ORDER BY u.ativo DESC, u.nome""")
        pessoas = cur.fetchall()
    return tela(request, "equipe_lista.html", usuario, pessoas=pessoas)


@router.get("/equipe/novo")
def novo_form(request: Request, usuario: dict = Depends(requer("usuarios.operacionais"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        papeis = _papeis_disponiveis(cur, usuario)
    return tela(request, "equipe_form.html", usuario, v={"vinculo": "FIXO"}, papeis=papeis)


@router.post("/equipe/novo")
def novo(request: Request, usuario: dict = Depends(requer("usuarios.operacionais")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    nome = (form.get("nome") or "").strip()[:80]
    login = (form.get("login") or "").strip().lower()[:40]
    vinculo = "FREELANCE" if form.get("vinculo") == "FREELANCE" else "FIXO"
    ate = _data(form.get("ate"))
    erro, uid = None, None
    with db.transacao(usuario_id=usuario["id"]) as cur:
        papeis = _papeis_disponiveis(cur, usuario)
    escolhidos = [p["codigo"] for p in papeis if form.get(f"papel_{p['codigo']}") == "1"]
    senha, erro = _senha_nova(form)
    if len(nome) < 2:
        erro = "Escreva o nome."
    elif not re.fullmatch(r"[a-z0-9._-]{3,40}", login):
        erro = "Login com 3 a 40 letras minúsculas, números, ponto ou traço (sem espaço e sem acento)."
    elif not escolhidos:
        erro = "Marque pelo menos um cargo."
    elif vinculo == "FREELANCE" and (not ate or ate < date.today()):
        erro = "Freelance precisa do último dia de acesso (hoje ou depois)."
    if not erro:
        try:
            with db.transacao(usuario_id=usuario["id"]) as cur:
                fone = re.sub(r"\D", "", form.get("telefone") or "")[:13] or None
                cur.execute("""INSERT INTO usuarios (loja_id, nome, login, senha_hash, vinculo, telefone_c, telefone_idx,
                                                     data_admissao, avisos)
                               VALUES (ctx_loja(), %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text""",
                            (nome, login, seguranca.gerar_hash(senha), vinculo, cripto.cifrar(fone, "usuarios.telefone"),
                             cripto.indice_telefone(fone), _data(form.get("data_admissao")), avisos_padrao(escolhidos)))
                uid = cur.fetchone()["id"]
                for codigo in escolhidos:
                    cur.execute("""INSERT INTO usuario_papeis (loja_id, usuario_id, papel_id, concedido_por, valido_ate)
                                   SELECT ctx_loja(), %s, id, %s, %s::date + 1 FROM papeis WHERE codigo = %s""",
                                (uid, usuario["id"], ate if vinculo == "FREELANCE" else None, codigo))
                auditar(cur, request, "funcionario_cadastrado", usuario["id"], "usuario", uid,
                        {"login": login, "cargos": escolhidos, "vinculo": vinculo})
        except psycopg.errors.UniqueViolation:
            erro = f"O login '{login}' já existe. Escolha outro."
        except psycopg.Error as e:
            erro = mensagem_do_banco(e)
    if erro:
        return tela(request, "equipe_form.html", usuario, v=form, papeis=papeis, erro=erro, status=400)
    return RedirectResponse(f"/equipe/{uid}?novo=1", status_code=303)


@router.get("/equipe/{uid}")
def detalhe(request: Request, uid: UUID, novo: int = 0, ok: str = "", usuario: dict = Depends(requer(*GERIR))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        pessoa = _pessoa(cur, uid)
        if not pessoa:
            return tela(request, "erro.html", usuario, status=404, titulo="Funcionário não encontrado")
        papeis = _papeis_disponiveis(cur, usuario)
        cur.execute("""SELECT to_char(a.criado_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM HH24:MI') AS quando, a.acao
                         FROM auditoria a WHERE a.usuario_id = %s ORDER BY a.id DESC LIMIT 10""", (uid,))
        atividade = cur.fetchall()
        cur.execute("""SELECT navegador, host(ip_ultimo) AS ip,
                              to_char(primeiro_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM/YYYY') AS desde,
                              to_char(ultimo_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM HH24:MI') AS ultimo
                         FROM usuario_aparelhos WHERE usuario_id = %s ORDER BY ultimo_em DESC LIMIT 10""", (uid,))
        aparelhos = cur.fetchall()
    return tela(request, "equipe_detalhe.html", usuario, p=pessoa, papeis=papeis, atividade=atividade, novo=novo,
                AVISOS=AVISOS, aparelhos=aparelhos,
                ok=ok, bloqueio=_pode_mexer(usuario, pessoa), gerir=pode(usuario, "usuarios.operacionais"))


def _acao(request, usuario, uid, form, faz):
    """Esqueleto das ações da tela do funcionário: confere permissão, executa, volta com recado."""
    checar_csrf(request, form.get("csrf"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        pessoa = _pessoa(cur, uid)
        if not pessoa:
            return RedirectResponse("/equipe", status_code=303)
        motivo = _pode_mexer(usuario, pessoa)
    if motivo:
        return tela(request, "erro.html", usuario, status=403, titulo="Não permitido", mensagem=motivo)
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            recado = faz(cur, pessoa)
    except (ValueError, psycopg.Error) as e:
        recado = str(e) if isinstance(e, ValueError) else mensagem_do_banco(e)
        with db.transacao(usuario_id=usuario["id"]) as cur:
            pessoa = _pessoa(cur, uid)
            papeis = _papeis_disponiveis(cur, usuario)
        return tela(request, "equipe_detalhe.html", usuario, p=pessoa, papeis=papeis, atividade=[], erro=recado,
                    AVISOS=AVISOS, aparelhos=[],
                    bloqueio=None, gerir=True, status=400)
    return RedirectResponse(f"/equipe/{uid}?ok={quote(recado)}", status_code=303)


@router.post("/equipe/{uid}/dados")
def dados(request: Request, uid: UUID, usuario: dict = Depends(requer(*GERIR)), form: dict = Depends(formulario)):
    def faz(cur, p):
        nome = (form.get("nome") or "").strip()[:80]
        if len(nome) < 2:
            raise ValueError("Escreva o nome.")
        fone = re.sub(r"\D", "", form.get("telefone") or "")[:13] or None
        if fone and len(fone) not in (10, 11):
            raise ValueError("WhatsApp com DDD, por exemplo (19) 99999-9999.")
        cur.execute("""UPDATE usuarios SET nome=%s, telefone_c=%s, telefone_idx=%s, data_admissao=%s, observacoes_c=%s
                        WHERE id=%s""",
                    (nome, cripto.cifrar(fone, "usuarios.telefone"), cripto.indice_telefone(fone),
                     _data(form.get("data_admissao")),
                     cripto.cifrar((form.get("observacoes") or "").strip()[:500] or None, "usuarios.observacoes"), uid))
        auditar(cur, request, "funcionario_editado", usuario["id"], "usuario", str(uid))
        return "Dados salvos."
    return _acao(request, usuario, uid, form, faz)


@router.post("/equipe/{uid}/cargos")
def cargos(request: Request, uid: UUID, usuario: dict = Depends(requer("usuarios.operacionais")),
           form: dict = Depends(formulario)):
    def faz(cur, p):
        vinculo = "FREELANCE" if form.get("vinculo") == "FREELANCE" else "FIXO"
        ate = _data(form.get("ate"))
        if vinculo == "FREELANCE" and (not ate or ate < date.today()):
            raise ValueError("Freelance precisa do último dia de acesso (hoje ou depois).")
        disponiveis = _papeis_disponiveis(cur, usuario)
        marcados = {x["codigo"] for x in disponiveis if form.get(f"papel_{x['codigo']}") == "1"}
        if not marcados and not (set(p["ativos"]) - {x["codigo"] for x in disponiveis}):
            raise ValueError("Marque pelo menos um cargo. Para tirar todo o acesso, use 'Desativar'.")
        cur.execute("UPDATE usuarios SET vinculo = %s WHERE id = %s", (vinculo, uid))
        for x in disponiveis:
            if x["codigo"] in marcados:
                cur.execute("""INSERT INTO usuario_papeis (loja_id, usuario_id, papel_id, concedido_por, valido_ate)
                               SELECT ctx_loja(), %s, id, %s, %s::date + 1 FROM papeis WHERE codigo = %s
                               ON CONFLICT (usuario_id, papel_id) DO UPDATE
                                 SET valido_ate = EXCLUDED.valido_ate, concedido_por = EXCLUDED.concedido_por,
                                     concedido_em = now()""",
                            (uid, usuario["id"], ate if vinculo == "FREELANCE" else None, x["codigo"]))
            elif x["codigo"] in p["ativos"]:
                cur.execute("""UPDATE usuario_papeis SET valido_ate = now()
                                WHERE usuario_id = %s AND papel_id = (SELECT id FROM papeis WHERE codigo = %s)""",
                            (uid, x["codigo"]))
        auditar(cur, request, "cargos_alterados", usuario["id"], "usuario", str(uid),
                {"cargos": sorted(marcados), "vinculo": vinculo, "ate": str(ate) if ate else None})
        return "Cargos atualizados."
    return _acao(request, usuario, uid, form, faz)


@router.post("/equipe/{uid}/ativo")
def ativo(request: Request, uid: UUID, usuario: dict = Depends(requer("usuarios.operacionais")),
          form: dict = Depends(formulario)):
    def faz(cur, p):
        novo = form.get("ativo") == "1"
        cur.execute("UPDATE usuarios SET ativo = %s, sessao_versao = sessao_versao + 1 WHERE id = %s", (novo, uid))
        auditar(cur, request, "funcionario_" + ("reativado" if novo else "desativado"), usuario["id"], "usuario", str(uid))
        return "Acesso liberado de novo." if novo else "Acesso desativado. A pessoa não entra mais no sistema."
    return _acao(request, usuario, uid, form, faz)


@router.post("/equipe/{uid}/senha")
def redefinir_senha(request: Request, uid: UUID, usuario: dict = Depends(requer("usuarios.operacionais")),
                    form: dict = Depends(formulario)):
    def faz(cur, p):
        senha, erro = _senha_nova(form, p.get("login"), p.get("nome"))
        if erro:
            raise ValueError(erro)
        cur.execute("""UPDATE usuarios SET senha_hash=%s, tentativas_falhas=0, bloqueado_ate=NULL,
                              sessao_versao = sessao_versao + 1 WHERE id=%s""",
                    (seguranca.gerar_hash(senha), uid))
        auditar(cur, request, "senha_redefinida", usuario["id"], "usuario", str(uid))
        return "Senha nova definida. Passe para a pessoa e peça para ela trocar em 'Minha senha'."
    return _acao(request, usuario, uid, form, faz)


@router.post("/equipe/{uid}/desbloquear")
def desbloquear(request: Request, uid: UUID, usuario: dict = Depends(requer("usuarios.operacionais")),
                form: dict = Depends(formulario)):
    def faz(cur, p):
        cur.execute("UPDATE usuarios SET tentativas_falhas=0, bloqueado_ate=NULL WHERE id=%s", (uid,))
        auditar(cur, request, "funcionario_desbloqueado", usuario["id"], "usuario", str(uid))
        return "Desbloqueado."
    return _acao(request, usuario, uid, form, faz)


# --------------------------------------------------------------------------- #
# Minha senha (qualquer pessoa)
# --------------------------------------------------------------------------- #
@router.get("/minha-senha")
def minha_senha_form(request: Request, usuario: dict = Depends(usuario_atual)):
    return tela(request, "minha_senha.html", usuario, obrigatoria=request.session.get("trocar_senha"))


@router.post("/minha-senha")
def minha_senha(request: Request, usuario: dict = Depends(usuario_atual), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("SELECT senha_hash FROM usuarios WHERE id = %s", (usuario["id"],))
        atual_ok = seguranca.verificar(cur.fetchone()["senha_hash"], form.get("atual") or "")
    senha, erro = _senha_nova(form, usuario.get("login"), usuario["nome"])
    if not atual_ok:
        erro = "A senha atual não confere."
    if not erro and senha == (form.get("atual") or ""):
        erro = "A senha nova precisa ser diferente da atual."
    if erro:
        return tela(request, "minha_senha.html", usuario, erro=erro, status=400,
                    obrigatoria=request.session.get("trocar_senha"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        # Trocar a senha desconecta os OUTROS aparelhos (este continua)
        cur.execute("""UPDATE usuarios SET senha_hash = %s, sessao_versao = sessao_versao + 1 WHERE id = %s
                       RETURNING sessao_versao""", (seguranca.gerar_hash(senha), usuario["id"]))
        request.session.pop("trocar_senha", None)
        nova_sessao(request, cur, usuario["id"], cur.fetchone()["sessao_versao"])   # troca o identificador da sessão
        auditar(cur, request, "senha_trocada", usuario["id"])
    return tela(request, "minha_senha.html", usuario, ok=True)


@router.post("/equipe/{uid}/avisos")
def avisos(request: Request, uid: UUID, usuario: dict = Depends(requer(*GERIR)), form: dict = Depends(formulario)):
    def faz(cur, p):
        marcados = sorted(k for k in AVISOS if form.get(f"aviso_{k}") == "1")
        cur.execute("UPDATE usuarios SET avisos = %s WHERE id = %s", (marcados, uid))
        auditar(cur, request, "avisos_alterados", usuario["id"], "usuario", str(uid), {"avisos": marcados})
        return "Avisos do WhatsApp atualizados."
    return _acao(request, usuario, uid, form, faz)


@router.post("/equipe/{uid}/desconectar")
def desconectar(request: Request, uid: UUID, usuario: dict = Depends(requer(*GERIR)), form: dict = Depends(formulario)):
    def faz(cur, p):
        cur.execute("UPDATE usuarios SET sessao_versao = sessao_versao + 1 WHERE id = %s", (uid,))
        auditar(cur, request, "sessoes_encerradas", usuario["id"], "usuario", str(uid))
        return "Desconectado de todos os aparelhos. Vai precisar entrar de novo."
    return _acao(request, usuario, uid, form, faz)
