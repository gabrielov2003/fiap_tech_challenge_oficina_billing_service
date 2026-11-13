import json
import os
from types import SimpleNamespace

import boto3
import pytest
from flask_jwt_extended import create_access_token
from moto import mock_aws

import application
from app import create_app
from ports import PagamentoPort

os.environ.update({
    'AWS_DEFAULT_REGION': 'us-east-1',
    'AWS_ACCESS_KEY_ID': 'testing',
    'AWS_SECRET_ACCESS_KEY': 'testing',
    'TABELA_ORCAMENTOS': 'orcamentos-teste',
    'FILA_COMANDOS': 'billing-comandos-teste.fifo',
    'FILA_EVENTOS_OS': 'os-eventos-teste.fifo',
    'SQS_ESPERA_SEGUNDOS': '0',
    'JWT_SECRET_KEY': 'chave-de-teste-com-mais-de-32-caracteres',
})


class PagamentosFalsos(PagamentoPort):
    def __init__(self):
        self.pagamentos = {}
        self.estornos = []
        self.indisponivel = False

    def criar_cobranca(self, id_os, valor_total):
        if self.indisponivel:
            raise RuntimeError('Mercado Pago fora do ar')
        return {'id': f'pref-{id_os}', 'link': f'https://mercadopago.teste/checkout/{id_os}'}

    def consultar_pagamento(self, id_pagamento):
        return self.pagamentos[id_pagamento]

    def estornar(self, id_pagamento):
        self.estornos.append(id_pagamento)

    def notificacao_autentica(self, cabecalhos, id_recurso):
        return cabecalhos.get('x-signature') != 'invalida'


class Ambiente(SimpleNamespace):
    def token(self, papel, id_usuario):
        with self.app.app_context():
            token = create_access_token(identity=str(id_usuario), additional_claims={'role': papel})
        return {'Authorization': f'Bearer {token}'}

    def enviar_comando(self, tipo, id_os, dados=None):
        application.mensageria.enviar(os.environ['FILA_COMANDOS'], {
            'tipo': tipo, 'id_os': id_os, 'dados': dados or {}, 'correlation_id': 'teste'
        })

    def eventos(self):
        url = self.sqs.get_queue_url(QueueName=os.environ['FILA_EVENTOS_OS'])['QueueUrl']
        mensagens = self.sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10).get('Messages', [])
        for mensagem in mensagens:
            self.sqs.delete_message(QueueUrl=url, ReceiptHandle=mensagem['ReceiptHandle'])
        return [json.loads(m['Body']) for m in mensagens]

    def gerar_orcamento(self, id_os, id_cliente=7, itens=None):
        self.enviar_comando('GerarOrcamento', id_os, {
            'id_cliente': id_cliente,
            'itens': itens or [
                {'descricao': 'Troca de óleo', 'tipo': 'servico', 'valor': 150},
                {'descricao': 'Filtro de óleo', 'tipo': 'peca', 'valor': 80.5},
            ],
        })
        application.processar_comandos()
        return self.eventos()


@pytest.fixture
def ambiente():
    with mock_aws():
        boto3.resource('dynamodb').create_table(
            TableName=os.environ['TABELA_ORCAMENTOS'],
            KeySchema=[{'AttributeName': 'id_os', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'id_os', 'AttributeType': 'N'}],
            BillingMode='PAY_PER_REQUEST',
        )
        sqs = boto3.client('sqs')
        for fila in (os.environ['FILA_COMANDOS'], os.environ['FILA_EVENTOS_OS']):
            sqs.create_queue(QueueName=fila, Attributes={'FifoQueue': 'true', 'ContentBasedDeduplication': 'true'})
        app = create_app()
        app.config['TESTING'] = True
        pagamentos = PagamentosFalsos()
        application.pagamentos = pagamentos
        yield Ambiente(app=app, cliente=app.test_client(), pagamentos=pagamentos, sqs=sqs)
