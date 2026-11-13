import os
from datetime import date, datetime
from decimal import Decimal

from flask import Flask
from flask.json.provider import DefaultJSONProvider
from flasgger import Swagger
from flask_jwt_extended import JWTManager

import application
from infrastructure import OrcamentoRepositoryDynamo, MercadoPagoGateway, MensageriaSQS, MetricasDatadog, para_json
from observabilidade import configurar_logs, registrar_rastreamento
from web import billing


class JSONProvider(DefaultJSONProvider):
    @staticmethod
    def default(o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, Decimal):
            return para_json(o)
        return DefaultJSONProvider.default(o)


def injetar_dependencias():
    application.orcamento_repo = OrcamentoRepositoryDynamo(os.getenv('TABELA_ORCAMENTOS', 'oficina-local-orcamentos'))
    application.pagamentos = MercadoPagoGateway(
        os.getenv('MP_ACCESS_TOKEN', ''), os.getenv('MP_WEBHOOK_SECRET', ''), os.getenv('URL_NOTIFICACAO', '')
    )
    application.mensageria = MensageriaSQS()
    application.metricas = MetricasDatadog()
    application.filas['comandos'] = os.getenv('FILA_COMANDOS', 'oficina-local-billing-comandos.fifo')
    application.filas['eventos_os'] = os.getenv('FILA_EVENTOS_OS', 'oficina-local-os-eventos.fifo')


def create_app():
    configurar_logs()

    app = Flask(__name__)
    app.json = JSONProvider(app)
    app.config['SWAGGER'] = {
        'title': 'Billing Service',
        'uiversion': 3,
        'specs_route': '/billing/apidocs/',
        'static_url_path': '/billing/flasgger_static',
        'specs': [{
            'endpoint': 'apispec_billing',
            'route': '/billing/apispec.json',
            'rule_filter': lambda rule: True,
            'model_filter': lambda tag: True,
        }],
    }
    app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', 'fiap-soat-key')

    JWTManager(app)
    Swagger(app, template={
        'info': {
            'title': 'Billing Service',
            'version': '4.0',
            'description': (
                'Orçamentos e pagamentos das ordens de serviço, com integração ao Mercado Pago. '
                'Use o token do cliente (POST /auth) ou do funcionário (POST /api/login) no botão Authorize.'
            )
        },
        'securityDefinitions': {
            'Bearer': {
                'type': 'apiKey',
                'name': 'Authorization',
                'in': 'header',
                'description': 'Informe `Bearer <token>`'
            }
        },
        'security': [{'Bearer': []}]
    })

    injetar_dependencias()
    registrar_rastreamento(app)
    app.register_blueprint(billing, url_prefix='/billing')

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(host=os.getenv('FLASK_HOST', '127.0.0.1'), port=5000, debug=os.getenv('FLASK_DEBUG', '0') == '1')
