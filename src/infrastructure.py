import hashlib
import hmac
import http.client
import json
import os
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError
from datadog.dogstatsd import DogStatsd

from ports import OrcamentoRepositoryPort, PagamentoPort, MensageriaPort, MetricasPort


def para_json(valor):
    if isinstance(valor, Decimal):
        return int(valor) if valor == valor.to_integral_value() else float(valor)
    raise TypeError(f'Tipo não serializável: {type(valor).__name__}')


def agora():
    return datetime.now(timezone.utc).isoformat()


def condicao_falhou(erro):
    return erro.response['Error']['Code'] == 'ConditionalCheckFailedException'


class OrcamentoRepositoryDynamo(OrcamentoRepositoryPort):
    def __init__(self, nome_tabela):
        self.tabela = boto3.resource('dynamodb').Table(nome_tabela)

    def buscar(self, id_os):
        return self.tabela.get_item(Key={'id_os': int(id_os)}).get('Item')

    def criar(self, orcamento):
        try:
            self.tabela.put_item(
                Item={
                    'id_os': orcamento.id_os, 'id_cliente': orcamento.id_cliente, 'itens': orcamento.itens,
                    'valor_total': orcamento.valor_total, 'status': orcamento.status,
                    'criado_em': agora(), 'atualizado_em': agora(),
                },
                ConditionExpression='attribute_not_exists(id_os)'
            )
            return True
        except ClientError as e:
            if condicao_falhou(e):
                return False
            raise

    def atualizar_status(self, id_os, status_atual, novo_status, campos=None):
        valores = {'status': novo_status, 'atualizado_em': agora(), **(campos or {})}
        try:
            self.tabela.update_item(
                Key={'id_os': int(id_os)},
                UpdateExpression='SET ' + ', '.join(f'#{campo} = :{campo}' for campo in valores),
                ConditionExpression='#status = :status_atual',
                ExpressionAttributeNames={f'#{campo}': campo for campo in valores},
                ExpressionAttributeValues={
                    **{f':{campo}': valor for campo, valor in valores.items()}, ':status_atual': status_atual
                },
            )
            return True
        except ClientError as e:
            if condicao_falhou(e):
                return False
            raise

    def ping(self):
        resposta = self.tabela.meta.client.describe_table(TableName=self.tabela.name)
        return resposta['Table']['TableStatus'] == 'ACTIVE'


class MercadoPagoGateway(PagamentoPort):
    HOST = 'api.mercadopago.com'

    def __init__(self, token, segredo_webhook='', url_notificacao=''):
        self.token = token
        self.segredo_webhook = segredo_webhook
        self.url_notificacao = url_notificacao

    def _chamar(self, metodo, caminho, corpo=None, chave_idempotencia=None):
        cabecalhos = {'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'}
        if chave_idempotencia:
            cabecalhos['X-Idempotency-Key'] = chave_idempotencia
        conexao = http.client.HTTPSConnection(self.HOST, timeout=10)
        try:
            conexao.request(metodo, caminho, json.dumps(corpo, default=para_json) if corpo is not None else None, cabecalhos)
            resposta = conexao.getresponse()
            conteudo = resposta.read()
        finally:
            conexao.close()
        if resposta.status >= 400:
            raise RuntimeError(f'Mercado Pago respondeu {resposta.status} em {metodo} {caminho}')
        return json.loads(conteudo or b'{}')

    def criar_cobranca(self, id_os, valor_total):
        corpo = {
            'items': [{
                'id': str(id_os), 'title': f'Ordem de serviço {id_os}', 'quantity': 1,
                'currency_id': 'BRL', 'unit_price': valor_total,
            }],
            'external_reference': str(id_os),
        }
        if self.url_notificacao:
            corpo['notification_url'] = self.url_notificacao
        preferencia = self._chamar('POST', '/checkout/preferences', corpo)
        link = preferencia.get('sandbox_init_point') if self.token.startswith('TEST-') else None
        return {'id': preferencia['id'], 'link': link or preferencia['init_point']}

    def consultar_pagamento(self, id_pagamento):
        pagamento = self._chamar('GET', f'/v1/payments/{id_pagamento}')
        referencia = str(pagamento.get('external_reference') or '')
        return {
            'id': str(pagamento['id']),
            'status': pagamento['status'],
            'id_os': int(referencia) if referencia.isdigit() else None,
        }

    def estornar(self, id_pagamento):
        self._chamar('POST', f'/v1/payments/{id_pagamento}/refunds', {}, f'estorno-{id_pagamento}')

    def notificacao_autentica(self, cabecalhos, id_recurso):
        if not self.segredo_webhook:
            return True
        partes = dict(
            parte.strip().split('=', 1) for parte in cabecalhos.get('x-signature', '').split(',') if '=' in parte
        )
        manifesto = f"id:{id_recurso.lower()};request-id:{cabecalhos.get('x-request-id', '')};ts:{partes.get('ts', '')};"
        esperado = hmac.new(self.segredo_webhook.encode(), manifesto.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(esperado, partes.get('v1', ''))


class MensageriaSQS(MensageriaPort):
    def __init__(self):
        self.sqs = boto3.client('sqs')
        self.urls = {}
        self.espera = int(os.getenv('SQS_ESPERA_SEGUNDOS', '10'))

    def _url(self, fila):
        if fila not in self.urls:
            self.urls[fila] = self.sqs.get_queue_url(QueueName=fila)['QueueUrl']
        return self.urls[fila]

    def enviar(self, fila, mensagem):
        self.sqs.send_message(
            QueueUrl=self._url(fila),
            MessageBody=json.dumps(mensagem, default=para_json),
            MessageGroupId=str(mensagem['id_os']),
        )

    def receber(self, fila):
        resposta = self.sqs.receive_message(
            QueueUrl=self._url(fila), MaxNumberOfMessages=10, WaitTimeSeconds=self.espera
        )
        return [(self._ler(m['Body']), m['ReceiptHandle']) for m in resposta.get('Messages', [])]

    def confirmar(self, fila, recibo):
        self.sqs.delete_message(QueueUrl=self._url(fila), ReceiptHandle=recibo)

    @staticmethod
    def _ler(corpo):
        try:
            mensagem = json.loads(corpo)
        except json.JSONDecodeError:
            return {}
        return mensagem if isinstance(mensagem, dict) else {}


def _tag(valor):
    return unicodedata.normalize('NFKD', valor).encode('ascii', 'ignore').decode().lower().replace(' ', '_')


class MetricasDatadog(MetricasPort):
    def __init__(self):
        self.statsd = DogStatsd(
            host=os.getenv('DD_AGENT_HOST', 'localhost'),
            port=int(os.getenv('DD_DOGSTATSD_PORT', '8125')),
            disable_buffering=True,
        )

    def orcamento_alterado(self, status):
        self.statsd.increment('oficina.billing.orcamentos', tags=[f'status:{_tag(status)}'])

    def erro_integracao(self, integracao, motivo):
        self.statsd.increment('oficina.integracao.erros', tags=[f'integracao:{integracao}', f'motivo:{motivo}'])
