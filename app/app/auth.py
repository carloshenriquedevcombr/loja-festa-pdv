"""Autenticação (quem você é) e autorização (o que pode fazer)."""
import ipaddress
import json

from fastapi import Depends, Request

from . import config, db


class NaoAutenticado(Exception):
    pass


class SemPermissao(Exception):
    pass


class TrocarSenha(Exception):
    """A senha atual não segue a regra nova: só libera o sistema depois de trocar."""


SQL_USUARIO = """
SELECT u.id::text, u.nome, u.ativo, u.sessao_versao, l.nome AS loja,
       COALESCE(array_agg(DISTINCT pe.codigo) FILTER (WHERE pe.codigo IS NOT NULL), '{}') AS permissoes,
       COALESCE(array_agg(DISTINCT pa.nome)   FILTER (WHERE pa.nome   IS NOT NULL), '{}') AS papeis
  FROM usuarios u
  JOIN lojas l ON l.id = u.loja_id
  LEFT JOIN usuario_papeis up ON up.usuario_id = u.id
                             AND (up.valido_ate IS NULL OR up.valido_ate > now())
  LEFT JOIN papeis pa           ON pa.id = up.papel_id
  LEFT JOIN papel_permissoes pp ON pp.papel_id = pa.id
  LEFT JOIN permissoes pe       ON pe.id = pp.permissao_id
 WHERE u.id = %s
 GROUP BY u.id, l.nome
"""
LIVRES_NA_TROCA = ("/minha-senha", "/sair", "/static")


def usuario_atual(request: Request) -> dict:
    """Recarrega o usuário a cada requisição: desativar alguém corta o acesso na hora.
    Também derruba a sessão parada há mais de SESSAO_OCIOSA_MIN minutos e a sessão de quem foi
    "desconectado de todos os aparelhos" (sessao_versao mudou)."""
    import time
    uid = request.session.get("usuario_id")
    if not uid or not config.LOJA_ID:
        raise NaoAutenticado
    agora = int(time.time())
    visto = request.session.get("visto", agora)
    if agora - visto > config.SESSAO_OCIOSA_MIN * 60:
        from . import sessao
        with db.transacao(usuario_id=uid) as cur:
            sessao.encerrar(cur, request.session.get("sid"), "PARADA")
        request.session.clear()
        request.session["aviso_login"] = "Você ficou um tempo sem usar e o sistema saiu sozinho. Entre de novo."
        raise NaoAutenticado
    from . import sessao
    ua = request.headers.get("user-agent")
    with db.transacao(usuario_id=uid) as cur:
        cur.execute(SQL_USUARIO, (uid,))
        usuario = cur.fetchone()
        motivo = sessao.conferir(cur, request.session.get("sid"), uid, aparelho_da_requisicao(request), ua, ip(request))
        if motivo in ("NAVEGADOR_DIFERENTE", "APARELHO_DIFERENTE") and usuario:
            from . import avisos
            avisos.para_equipe(cur, "seguranca",
                               f"🚨 Alguém tentou usar a sessão de {usuario['nome']} em outro "
                               f"{'navegador' if motivo == 'NAVEGADOR_DIFERENTE' else 'aparelho'} (IP {ip(request) or '?'}). "
                               "A sessão foi encerrada; a pessoa precisa entrar de novo.", "seguranca", None)
            cur.execute("""INSERT INTO auditoria (loja_id, usuario_id, acao, entidade, dados, ip)
                           VALUES (ctx_loja(), %s, 'sessao_roubada', 'sessao', jsonb_build_object('motivo', %s::text), %s::inet)""",
                        (uid, motivo, ip(request)))
    if (motivo or not usuario or not usuario["ativo"] or not usuario["permissoes"]
            or request.session.get("versao", 1) != usuario["sessao_versao"]):
        request.session.clear()
        raise NaoAutenticado
    request.session["visto"] = agora
    if request.session.get("trocar_senha") and not request.url.path.startswith(LIVRES_NA_TROCA):
        raise TrocarSenha
    usuario["permissoes"] = set(usuario["permissoes"])
    return usuario


def aparelho_da_requisicao(request: Request) -> str | None:
    from .acesso import COOKIE_APARELHO, hash_aparelho
    return hash_aparelho(request.cookies.get(COOKIE_APARELHO))


def nova_sessao(request: Request, cur, uid: str, versao: int, aparelho: str | None = None) -> None:
    """Abre a sessão no banco e guarda o identificador no cookie cifrado (troca a cada entrada/troca de senha)."""
    from . import acesso, sessao
    antigo = request.session.get("sid")
    manter = {k: request.session[k] for k in ("trocar_senha",) if k in request.session}
    request.session.clear()                      # nova sessão: evita fixação de sessão
    if antigo:
        sessao.encerrar(cur, antigo, "SAIU")
    import time
    sid = sessao.abrir(cur, uid, aparelho or aparelho_da_requisicao(request),
                       request.headers.get("user-agent"), acesso.descrever_navegador(request.headers.get("user-agent")),
                       ip(request), config.SESSAO_HORAS)
    request.session.update({"usuario_id": uid, "versao": versao, "visto": int(time.time()), "sid": sid, **manter})


def requer(*permissoes: str):
    """Exige pelo menos uma das permissões. Uso: u: dict = Depends(requer("caixa.entrada"))"""
    def verificar(usuario: dict = Depends(usuario_atual)) -> dict:
        if not any(p in usuario["permissoes"] for p in permissoes):
            raise SemPermissao
        return usuario
    return verificar


def pode(usuario: dict, *permissoes: str) -> bool:
    return any(p in usuario["permissoes"] for p in permissoes)


# Faixas de IP da Cloudflare (cloudflare.com/ips). Só quem vem de uma delas pode dizer "o IP real é X".
CLOUDFLARE_REDES = [ipaddress.ip_network(r) for r in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
    "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22", "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32",
    "2405:b500::/32", "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32")]


def _valido(texto: str | None):
    try:
        return ipaddress.ip_address(texto.strip()) if texto else None
    except ValueError:
        return None


def ip(request: Request) -> str | None:
    """IP de quem está do outro lado, sem acreditar em cabeçalho inventado.

    * PC local: o IP da conexão.
    * VPS (ATRAS_DO_PROXY=1): o ÚLTIMO endereço do X-Forwarded-For, que foi o próprio Nginx Proxy Manager
      que escreveu. Os anteriores vêm do navegador e qualquer um inventa ("X-Forwarded-For: 1.2.3.4").
    * Cloudflare (CLOUDFLARE=1): o CF-Connecting-IP, mas SÓ se a conexão veio de um IP da Cloudflare.
      Quem chega direto na VPS fingindo ser a Cloudflare é ignorado.
    """
    visto = _valido(request.client.host if request.client else None)
    if config.ATRAS_DO_PROXY:
        cadeia = [x for x in (request.headers.get("x-forwarded-for") or "").split(",") if x.strip()]
        visto = _valido(cadeia[-1]) if cadeia else visto
    if config.CLOUDFLARE and visto and any(visto in rede for rede in CLOUDFLARE_REDES):
        visto = _valido(request.headers.get("cf-connecting-ip")) or visto
    return str(visto) if visto else None


def veio_da_cloudflare(request: Request) -> bool:
    visto = _valido(request.client.host if request.client else None)
    if config.ATRAS_DO_PROXY:
        cadeia = [x for x in (request.headers.get("x-forwarded-for") or "").split(",") if x.strip()]
        visto = _valido(cadeia[-1]) if cadeia else visto
    return bool(visto and any(visto in rede for rede in CLOUDFLARE_REDES))


def auditar(cur, request: Request, acao: str, usuario_id: str | None,
            entidade: str = "sessao", entidade_id: str | None = None, dados: dict | None = None):
    cur.execute(
        """INSERT INTO auditoria (loja_id, usuario_id, acao, entidade, entidade_id, dados, ip, request_id)
           VALUES (ctx_loja(), %s, %s, %s, %s, %s::jsonb, %s::inet, %s)""",
        (usuario_id, acao, entidade, entidade_id,
         json.dumps(dados, ensure_ascii=False) if dados else None,
         ip(request), getattr(request.state, "request_id", None)),
    )
