import json
import logging
import os
import time
import uuid

from flask import g, has_request_context, request

ATRIBUTOS_PADRAO = set(vars(logging.makeLogRecord({}))) | {'message', 'asctime'}
ROTAS_SEM_LOG = {'/billing/health', '/billing/ready'}


class FiltroCorrelacao(logging.Filter):
    def filter(self, record):
        if has_request_context() and not hasattr(record, 'correlation_id'):
            record.correlation_id = g.get('correlation_id')
        return True


class FormatadorJSON(logging.Formatter):
    def format(self, record):
        dados = {
            'timestamp': self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z'),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        dados.update({chave: valor for chave, valor in vars(record).items() if chave not in ATRIBUTOS_PADRAO})
        if record.exc_info:
            dados['error.stack'] = self.formatException(record.exc_info)
        return json.dumps(dados, default=str, ensure_ascii=False)


def configurar_logs():
    handler = logging.StreamHandler()
    handler.setFormatter(FormatadorJSON())
    handler.addFilter(FiltroCorrelacao())
    raiz = logging.getLogger()
    raiz.handlers = [handler]
    raiz.setLevel(os.getenv('LOG_LEVEL', 'INFO'))
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('datadog.dogstatsd').setLevel(logging.ERROR)


def registrar_rastreamento(app):
    logger = logging.getLogger('oficina.http')

    @app.before_request
    def iniciar_requisicao():
        g.correlation_id = request.headers.get('X-Correlation-ID') or str(uuid.uuid4())
        g.inicio = time.perf_counter()

    @app.after_request
    def finalizar_requisicao(resposta):
        resposta.headers['X-Correlation-ID'] = g.correlation_id
        if request.path not in ROTAS_SEM_LOG:
            logger.info('requisicao', extra={
                'http.method': request.method,
                'http.url_details.path': request.path,
                'http.status_code': resposta.status_code,
                'duration_ms': round((time.perf_counter() - g.inicio) * 1000, 2),
            })
        return resposta
