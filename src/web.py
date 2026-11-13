from functools import wraps

from flask import Blueprint, request, jsonify, g
from flask_jwt_extended import jwt_required, get_jwt, get_jwt_identity
from flasgger import swag_from

from application import BillingService, IntegracaoIndisponivel

billing = Blueprint('billing', __name__)

ADMIN = 'admin'
CLIENTE = 'cliente'


def papel_requerido(*papeis):
    def decorator(fn):
        @wraps(fn)
        @jwt_required()
        def wrapper(*args, **kwargs):
            if get_jwt().get('role') not in papeis:
                return jsonify({"erro": "Acesso negado para este perfil"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def orcamento_do_usuario(id_os):
    orcamento = BillingService.buscar_orcamento(id_os)
    if not orcamento:
        return None, (jsonify({"erro": "Orçamento não encontrado"}), 404)
    if get_jwt().get('role') == CLIENTE and orcamento['id_cliente'] != int(get_jwt_identity()):
        return None, (jsonify({"erro": "Acesso negado a este orçamento"}), 403)
    return orcamento, None


@billing.route('/orcamentos/<int:id_os>', methods=['GET'])
@papel_requerido(ADMIN, CLIENTE)
@swag_from({
    'tags': ['Orçamentos'],
    'description': 'Consulta o orçamento de uma OS. Com token de cliente, só o dono da OS consegue consultar.',
    'parameters': [{'name': 'id_os', 'in': 'path', 'type': 'integer', 'required': True, 'example': 1}],
    'responses': {
        200: {'description': 'Orçamento com itens, valor total, status e link de pagamento'},
        403: {'description': 'Orçamento de outro cliente'},
        404: {'description': 'Orçamento não encontrado'}
    }
})
def get_orcamento(id_os):
    orcamento, erro = orcamento_do_usuario(id_os)
    if erro:
        return erro
    return jsonify(orcamento), 200


@billing.route('/orcamentos/<int:id_os>/aprovacao', methods=['POST'])
@papel_requerido(ADMIN, CLIENTE)
@swag_from({
    'tags': ['Orçamentos'],
    'description': (
        'Aprova ou recusa o orçamento. Na aprovação, gera a cobrança no Mercado Pago e devolve o link de pagamento. '
        'Com token de cliente, só o dono da OS pode aprovar.'
    ),
    'parameters': [
        {'name': 'id_os', 'in': 'path', 'type': 'integer', 'required': True, 'example': 1},
        {
            'name': 'body', 'in': 'body', 'required': True,
            'schema': {
                'type': 'object',
                'required': ['aprovado'],
                'properties': {'aprovado': {'type': 'boolean', 'example': True}}
            }
        }
    ],
    'responses': {
        200: {'description': 'Orçamento aprovado (Aguardando pagamento, com link) ou recusado'},
        400: {'description': 'Campo ausente ou orçamento fora do status esperado'},
        403: {'description': 'Orçamento de outro cliente'},
        404: {'description': 'Orçamento não encontrado'},
        503: {'description': 'Mercado Pago indisponível'}
    }
})
def aprovar_orcamento(id_os):
    data = request.json or {}
    if 'aprovado' not in data:
        return jsonify({"erro": "'aprovado' é obrigatório"}), 400
    orcamento, erro = orcamento_do_usuario(id_os)
    if erro:
        return erro
    try:
        resultado = BillingService.aprovar(orcamento, bool(data['aprovado']), g.get('correlation_id'))
    except IntegracaoIndisponivel as e:
        return jsonify({"erro": str(e)}), 503
    if 'erro' in resultado:
        return jsonify(resultado), 400
    return jsonify(resultado), 200


@billing.route('/webhooks/mercadopago', methods=['POST'])
@swag_from({
    'tags': ['Webhook'],
    'description': (
        'Notificação de pagamento do Mercado Pago. A assinatura é validada pelo header x-signature, '
        'e o status é sempre confirmado consultando o pagamento na API do Mercado Pago.'
    ),
    'security': [],
    'parameters': [
        {'name': 'type', 'in': 'query', 'type': 'string', 'example': 'payment'},
        {'name': 'data.id', 'in': 'query', 'type': 'string', 'example': '123456789'},
        {
            'name': 'body', 'in': 'body', 'required': False,
            'schema': {
                'type': 'object',
                'properties': {
                    'type': {'type': 'string', 'example': 'payment'},
                    'data': {'type': 'object', 'properties': {'id': {'type': 'string', 'example': '123456789'}}}
                }
            }
        }
    ],
    'responses': {
        200: {'description': 'Notificação processada ou ignorada'},
        401: {'description': 'Assinatura inválida'}
    }
})
def webhook_mercadopago():
    data = request.get_json(silent=True) or {}
    tipo = request.args.get('type') or request.args.get('topic') or data.get('type')
    id_pagamento = str(request.args.get('data.id') or request.args.get('id') or (data.get('data') or {}).get('id') or '')
    if tipo != 'payment' or not id_pagamento.isdigit():
        return jsonify({"mensagem": "Notificação ignorada"}), 200
    if not BillingService.notificacao_autentica(request.headers, id_pagamento):
        return jsonify({"erro": "Assinatura inválida"}), 401
    BillingService.processar_pagamento(id_pagamento, g.get('correlation_id'))
    return jsonify({"mensagem": "Notificação processada"}), 200


@billing.route('/health', methods=['GET'])
@swag_from({
    'tags': ['Saúde'],
    'description': 'Liveness: indica que o serviço está no ar',
    'security': [],
    'responses': {200: {'description': 'Serviço no ar'}}
})
def health():
    return jsonify({"status": "ok"}), 200


@billing.route('/ready', methods=['GET'])
@swag_from({
    'tags': ['Saúde'],
    'description': 'Readiness: indica que o serviço consegue acessar a tabela no DynamoDB',
    'security': [],
    'responses': {
        200: {'description': 'Pronto para receber tráfego'},
        503: {'description': 'Banco de dados indisponível'}
    }
})
def ready():
    if not BillingService.banco_disponivel():
        return jsonify({"status": "indisponivel"}), 503
    return jsonify({"status": "ok"}), 200
