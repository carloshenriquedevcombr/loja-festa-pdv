"""Logs estruturados em JSON (um evento por linha), prontos para `docker logs`."""
import json
import logging
import os
import sys
from datetime import datetime, timezone

_CAMPOS_EXTRAS = ("request_id", "usuario_id", "metodo", "caminho", "status", "duracao_ms", "evento")


class FormatoJSON(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        dados = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "nivel": record.levelname,
            "msg": record.getMessage(),
        }
        for campo in _CAMPOS_EXTRAS:
            valor = getattr(record, campo, None)
            if valor is not None:
                dados[campo] = valor
        if record.exc_info:
            dados["erro"] = self.formatException(record.exc_info)
        return json.dumps(dados, ensure_ascii=False)


def configurar() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(FormatoJSON())
    raiz = logging.getLogger()
    raiz.handlers = [handler]
    raiz.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    return logging.getLogger("loja")
