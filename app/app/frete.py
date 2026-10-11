"""Entrega da sacola do site.

* Retirada na loja (sem custo).
* Entrega local por motoboy ou aplicativo (99, Uber Flash…): só nas cidades que a Dona cadastrou, pelo valor dela.
* Envio para outras cidades pelo Melhor Envio (Correios/transportadoras) — só para compra (aluguel precisa voltar).
  A cotação usa o token da loja (colado em Configurações > Sacola e entrega, guardado cifrado; ou
  MELHOR_ENVIO_TOKEN no .env) e o pacote padrão das Configurações.
"""
import json
import re
import urllib.error
import urllib.request

from . import config


class ErroFrete(Exception):
    pass


def cep_limpo(texto: str | None) -> str | None:
    d = re.sub(r"\D", "", texto or "")
    return d if len(d) == 8 else None


def cidades(cur, aluguel: bool = False) -> list[dict]:
    cur.execute(f"""SELECT id::text, cidade, uf, meio, valor_centavos, prazo, aluguel FROM entrega_cidades
                     WHERE ativa {'AND aluguel' if aluguel else ''} ORDER BY cidade""")
    return cur.fetchall()


API_PRODUCAO = "https://melhorenvio.com.br"
API_SANDBOX = "https://sandbox.melhorenvio.com.br"


def acesso(cur) -> dict | None:
    """Token e endereço da API do Melhor Envio da loja: primeiro o colado na tela (cifrado), senão o do .env."""
    cur.execute("SELECT token_frete(%s) AS t, (SELECT sandbox FROM frete_config) AS sandbox", (config.CHAVE_TOKENS,))
    linha = cur.fetchone() or {}
    if linha.get("t"):
        api = config.MELHOR_ENVIO_API_FIXA or (API_SANDBOX if linha.get("sandbox") else API_PRODUCAO)
        return {"token": linha["t"], "api": api}
    if config.MELHOR_ENVIO_TOKEN:
        return {"token": config.MELHOR_ENVIO_TOKEN, "api": config.MELHOR_ENVIO_API}
    return None


def envio_disponivel(loja: dict) -> bool:
    return bool(loja.get("entrega_envio") and loja.get("cep_origem") and loja.get("me"))


def _chamar(metodo: str, caminho: str, corpo: dict | None, me: dict):
    pedido = urllib.request.Request(
        me["api"] + caminho, method=metodo,
        data=json.dumps(corpo).encode() if corpo is not None else None,
        headers={"Authorization": f"Bearer {me['token']}", "Accept": "application/json",
                 "Content-Type": "application/json", "User-Agent": "loja-festa (contato pelo site da loja)"})
    try:
        # API do Melhor Envio (https fixo ou do .env).
        with urllib.request.urlopen(pedido, timeout=15) as r:  # nosec B310
            return json.loads(r.read(2_000_000).decode() or "null")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ErroFrete("O Melhor Envio recusou o token da loja.") from e
        raise ErroFrete("O cálculo do frete não respondeu agora. Tente de novo em instantes.") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise ErroFrete("Não consegui calcular o frete agora. Tente de novo em instantes.") from e


def cotar(loja: dict, cep_destino: str, valor_centavos: int, quantidade: int = 1) -> list[dict]:
    """Opções de envio (mais barata primeiro). Cada uma: id, nome, empresa, preco_centavos, prazo_dias."""
    if not envio_disponivel(loja):
        raise ErroFrete("Envio para outras cidades ainda não está disponível.")
    corpo = {"from": {"postal_code": loja["cep_origem"]}, "to": {"postal_code": cep_destino},
             "products": [{"id": "pecas", "width": loja["pacote_largura_cm"], "height": loja["pacote_altura_cm"],
                           "length": loja["pacote_comprimento_cm"], "weight": round(loja["pacote_peso_g"] / 1000, 3),
                           "insurance_value": round(valor_centavos / 100, 2), "quantity": max(1, quantidade)}]}
    resposta = _chamar("POST", "/api/v2/me/shipment/calculate", corpo, loja["me"])
    if not isinstance(resposta, list):
        raise ErroFrete("Resposta estranha do cálculo de frete.")
    opcoes = []
    for s in resposta[:20]:
        if not isinstance(s, dict) or s.get("error") or s.get("price") in (None, ""):
            continue
        try:
            preco = round(float(s.get("custom_price") or s["price"]) * 100)
            sid = int(s["id"])
            prazo = int((s.get("custom_delivery_time") or s.get("delivery_time") or 0))
        except (TypeError, ValueError, KeyError):
            continue
        empresa = str((s.get("company") or {}).get("name") or "")[:40]
        opcoes.append({"id": sid, "nome": f"{empresa} {str(s.get('name') or '')}".strip()[:80],
                       "empresa": empresa, "preco_centavos": preco, "prazo_dias": prazo})
    return sorted(opcoes, key=lambda o: (o["preco_centavos"], o["prazo_dias"]))


def rastrear(etiqueta_id: str, me: dict | None) -> str | None:
    """Código de rastreio de uma etiqueta já gerada no painel do Melhor Envio (ou None)."""
    if not me or not re.fullmatch(r"[A-Za-z0-9-]{6,80}", etiqueta_id or ""):
        return None
    r = _chamar("POST", "/api/v2/me/shipment/tracking", {"orders": [etiqueta_id]}, me)
    info = (r or {}).get(etiqueta_id) if isinstance(r, dict) else None
    codigo = (info or {}).get("tracking") or (info or {}).get("melhorenvio_tracking")
    return str(codigo)[:60] if codigo else None


def testar(me: dict) -> str:
    """Confere o token: devolve o nome/e-mail da conta no Melhor Envio (ou levanta ErroFrete)."""
    r = _chamar("GET", "/api/v2/me", None, me)
    if not isinstance(r, dict):
        raise ErroFrete("Resposta estranha do Melhor Envio.")
    nome = " ".join(str(r.get(k) or "") for k in ("firstname", "lastname")).strip() or str(r.get("name") or "")
    return (nome + (f" · {r.get('email')}" if r.get("email") else "")).strip()[:120] or "Conta conectada"
