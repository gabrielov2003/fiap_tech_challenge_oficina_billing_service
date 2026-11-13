import hashlib
import hmac
import json

import pytest

import application
import infrastructure
from domain import Orcamento, DomainError
from infrastructure import MercadoPagoGateway, para_json


def pagamento(ambiente, id_pagamento, status, id_os):
    ambiente.pagamentos.pagamentos[id_pagamento] = {'id': id_pagamento, 'status': status, 'id_os': id_os}


def notificar(ambiente, id_pagamento, cabecalhos=None):
    return ambiente.cliente.post(
        f'/billing/webhooks/mercadopago?type=payment&data.id={id_pagamento}',
        json={'type': 'payment', 'data': {'id': id_pagamento}},
        headers=cabecalhos or {},
    )


def aprovar(ambiente, id_os, aprovado=True, papel='cliente', id_usuario=7):
    return ambiente.cliente.post(
        f'/billing/orcamentos/{id_os}/aprovacao', json={'aprovado': aprovado},
        headers=ambiente.token(papel, id_usuario),
    )


def test_gera_orcamento_e_publica_evento(ambiente):
    eventos = ambiente.gerar_orcamento(1)
    assert [e['tipo'] for e in eventos] == ['OrcamentoGerado']
    assert eventos[0]['dados']['valor_total'] == 230.5
    assert eventos[0]['correlation_id'] == 'teste'

    orcamento = ambiente.cliente.get('/billing/orcamentos/1', headers=ambiente.token('admin', 1)).get_json()
    assert orcamento['status'] == 'Aguardando aprovação'
    assert len(orcamento['itens']) == 2


def test_comando_repetido_reemite_o_evento_sem_duplicar(ambiente):
    ambiente.gerar_orcamento(2)
    eventos = ambiente.gerar_orcamento(2)
    assert [e['tipo'] for e in eventos] == ['OrcamentoGerado']


def test_orcamento_sem_itens_falha(ambiente):
    ambiente.enviar_comando('GerarOrcamento', 3, {'id_cliente': 7, 'itens': []})
    application.processar_comandos()
    eventos = ambiente.eventos()
    assert eventos[0]['tipo'] == 'OrcamentoFalhou'
    assert application.orcamento_repo.buscar(3) is None


def test_comando_desconhecido_e_descartado(ambiente):
    ambiente.enviar_comando('ComandoQueNaoExiste', 4)
    application.processar_comandos()
    application.processar_comandos()
    assert ambiente.eventos() == []


def test_falha_inesperada_mantem_a_mensagem_na_fila(ambiente, monkeypatch):
    def quebrar(*args):
        raise RuntimeError('DynamoDB fora do ar')

    monkeypatch.setattr(application.orcamento_repo, 'criar', quebrar)
    ambiente.enviar_comando('GerarOrcamento', 5, {
        'id_cliente': 7, 'itens': [{'descricao': 'Revisão', 'tipo': 'servico', 'valor': 300}]
    })
    application.processar_comandos()
    assert ambiente.eventos() == []

    monkeypatch.undo()
    url = ambiente.sqs.get_queue_url(QueueName='billing-comandos-teste.fifo')['QueueUrl']
    atributos = ambiente.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=['All'])['Attributes']
    assert int(atributos['ApproximateNumberOfMessagesNotVisible']) == 1


def test_consulta_de_orcamento_respeita_o_dono(ambiente):
    ambiente.gerar_orcamento(10, id_cliente=7)
    assert ambiente.cliente.get('/billing/orcamentos/10', headers=ambiente.token('cliente', 7)).status_code == 200
    assert ambiente.cliente.get('/billing/orcamentos/10', headers=ambiente.token('cliente', 8)).status_code == 403
    assert ambiente.cliente.get('/billing/orcamentos/999', headers=ambiente.token('admin', 1)).status_code == 404
    assert ambiente.cliente.get('/billing/orcamentos/10').status_code == 401
    assert ambiente.cliente.get('/billing/orcamentos/10', headers=ambiente.token('outro', 1)).status_code == 403


def test_aprovacao_gera_link_de_pagamento(ambiente):
    ambiente.gerar_orcamento(11)
    resposta = aprovar(ambiente, 11)
    assert resposta.status_code == 200
    assert resposta.get_json()['status'] == 'Aguardando pagamento'
    assert resposta.get_json()['link_pagamento'] == 'https://mercadopago.teste/checkout/11'
    assert [e['tipo'] for e in ambiente.eventos()] == ['OrcamentoAprovado']

    repetida = aprovar(ambiente, 11)
    assert repetida.status_code == 200
    assert [e['tipo'] for e in ambiente.eventos()] == ['OrcamentoAprovado']


def test_recusa_do_orcamento(ambiente):
    ambiente.gerar_orcamento(12)
    resposta = aprovar(ambiente, 12, aprovado=False)
    assert resposta.get_json()['status'] == 'Recusado'
    assert [e['tipo'] for e in ambiente.eventos()] == ['OrcamentoRecusado']
    assert aprovar(ambiente, 12).status_code == 400


def test_aprovacao_valida_campos_e_dono(ambiente):
    ambiente.gerar_orcamento(13, id_cliente=7)
    sem_campo = ambiente.cliente.post('/billing/orcamentos/13/aprovacao', json={}, headers=ambiente.token('cliente', 7))
    assert sem_campo.status_code == 400
    assert aprovar(ambiente, 13, id_usuario=8).status_code == 403
    assert aprovar(ambiente, 999).status_code == 404


def test_aprovacao_com_mercado_pago_fora_do_ar(ambiente):
    ambiente.gerar_orcamento(14)
    ambiente.pagamentos.indisponivel = True
    assert aprovar(ambiente, 14).status_code == 503
    assert application.orcamento_repo.buscar(14)['status'] == 'Aguardando aprovação'


def test_pagamento_aprovado_pelo_webhook(ambiente):
    ambiente.gerar_orcamento(20)
    aprovar(ambiente, 20)
    ambiente.eventos()
    pagamento(ambiente, '9001', 'approved', 20)

    assert notificar(ambiente, '9001').status_code == 200
    eventos = ambiente.eventos()
    assert [e['tipo'] for e in eventos] == ['PagamentoAprovado']
    orcamento = application.orcamento_repo.buscar(20)
    assert orcamento['status'] == 'Pago'
    assert orcamento['pagamento_id'] == '9001'

    notificar(ambiente, '9001')
    assert [e['tipo'] for e in ambiente.eventos()] == ['PagamentoAprovado']
    assert ambiente.pagamentos.estornos == []


def test_pagamento_recusado_cancela_o_orcamento(ambiente):
    ambiente.gerar_orcamento(21)
    aprovar(ambiente, 21)
    ambiente.eventos()
    pagamento(ambiente, '9002', 'rejected', 21)

    notificar(ambiente, '9002')
    assert [e['tipo'] for e in ambiente.eventos()] == ['PagamentoRecusado']
    assert application.orcamento_repo.buscar(21)['status'] == 'Cancelado'


def test_pagamento_aprovado_depois_do_cancelamento_e_estornado(ambiente):
    ambiente.gerar_orcamento(22)
    aprovar(ambiente, 22)
    pagamento(ambiente, '9003', 'rejected', 22)
    notificar(ambiente, '9003')
    pagamento(ambiente, '9004', 'approved', 22)

    notificar(ambiente, '9004')
    assert ambiente.pagamentos.estornos == ['9004']
    assert application.orcamento_repo.buscar(22)['status'] == 'Cancelado'


def test_pagamento_pendente_ou_sem_orcamento_e_ignorado(ambiente):
    ambiente.gerar_orcamento(23)
    aprovar(ambiente, 23)
    ambiente.eventos()
    pagamento(ambiente, '9005', 'in_process', 23)
    pagamento(ambiente, '9006', 'approved', None)

    assert notificar(ambiente, '9005').status_code == 200
    assert notificar(ambiente, '9006').status_code == 200
    assert ambiente.eventos() == []
    assert application.orcamento_repo.buscar(23)['status'] == 'Aguardando pagamento'


def test_webhook_ignora_outros_tipos_e_valida_assinatura(ambiente):
    ignorada = ambiente.cliente.post('/billing/webhooks/mercadopago?type=merchant_order&data.id=1', json={})
    assert ignorada.get_json()['mensagem'] == 'Notificação ignorada'
    sem_id = ambiente.cliente.post('/billing/webhooks/mercadopago', json={'type': 'payment', 'data': {'id': 'abc'}})
    assert sem_id.get_json()['mensagem'] == 'Notificação ignorada'
    assert notificar(ambiente, '9007', {'x-signature': 'invalida'}).status_code == 401


def test_estorno_como_compensacao(ambiente):
    ambiente.gerar_orcamento(30)
    aprovar(ambiente, 30)
    pagamento(ambiente, '9010', 'approved', 30)
    notificar(ambiente, '9010')
    ambiente.eventos()

    ambiente.enviar_comando('EstornarPagamento', 30, {'motivo': 'Peça indisponível'})
    application.processar_comandos()
    assert ambiente.pagamentos.estornos == ['9010']
    assert [e['tipo'] for e in ambiente.eventos()] == ['PagamentoEstornado']
    orcamento = application.orcamento_repo.buscar(30)
    assert orcamento['status'] == 'Estornado'
    assert orcamento['motivo_estorno'] == 'Peça indisponível'

    ambiente.enviar_comando('EstornarPagamento', 30, {'motivo': 'Repetido'})
    application.processar_comandos()
    assert ambiente.pagamentos.estornos == ['9010']
    assert [e['tipo'] for e in ambiente.eventos()] == ['PagamentoEstornado']


def test_estorno_ignorado_sem_pagamento_e_invalido_sem_orcamento(ambiente):
    ambiente.gerar_orcamento(31)
    ambiente.eventos()
    ambiente.enviar_comando('EstornarPagamento', 31)
    ambiente.enviar_comando('EstornarPagamento', 999)
    application.processar_comandos()
    assert ambiente.pagamentos.estornos == []
    assert ambiente.eventos() == []


def test_health_ready_e_correlation_id(ambiente, monkeypatch):
    assert ambiente.cliente.get('/billing/health').status_code == 200
    assert ambiente.cliente.get('/billing/ready').status_code == 200
    resposta = ambiente.cliente.get('/billing/health', headers={'X-Correlation-ID': 'abc'})
    assert resposta.headers['X-Correlation-ID'] == 'abc'

    def quebrar():
        raise RuntimeError('fora do ar')

    monkeypatch.setattr(application.orcamento_repo, 'ping', quebrar)
    assert ambiente.cliente.get('/billing/ready').status_code == 503


def test_swagger_no_caminho_do_servico(ambiente):
    assert ambiente.cliente.get('/billing/apidocs/').status_code == 200
    spec = ambiente.cliente.get('/billing/apispec.json').get_json()
    assert '/billing/orcamentos/{id_os}/aprovacao' in spec['paths']


def test_dominio_valida_itens():
    with pytest.raises(DomainError):
        Orcamento(1, 7, [{'descricao': 'Sem valor', 'tipo': 'servico'}])
    with pytest.raises(DomainError):
        Orcamento(1, 7, [{'descricao': 'Negativo', 'tipo': 'peca', 'valor': -1}])
    with pytest.raises(DomainError):
        Orcamento(1, 7, [{'descricao': 'Zero', 'tipo': 'peca', 'valor': 0}])
    assert Orcamento.pode_transicionar('Pago', 'Estornado')
    assert not Orcamento.pode_transicionar('Recusado', 'Pago')


def test_para_json():
    from decimal import Decimal
    assert para_json(Decimal('10')) == 10
    assert para_json(Decimal('10.5')) == 10.5
    with pytest.raises(TypeError):
        para_json(object())


class ConexaoFalsa:
    respostas = []
    chamadas = []

    def __init__(self, host, timeout):
        self.host = host

    def request(self, metodo, caminho, corpo, cabecalhos):
        ConexaoFalsa.chamadas.append((metodo, caminho, json.loads(corpo) if corpo else None, cabecalhos))

    def getresponse(self):
        status, corpo = ConexaoFalsa.respostas.pop(0)
        return type('Resposta', (), {'status': status, 'read': lambda self: json.dumps(corpo).encode()})()

    def close(self):
        pass


@pytest.fixture
def mercado_pago(monkeypatch):
    ConexaoFalsa.respostas = []
    ConexaoFalsa.chamadas = []
    monkeypatch.setattr(infrastructure.http.client, 'HTTPSConnection', ConexaoFalsa)
    return MercadoPagoGateway('TEST-123', 'segredo', 'https://gateway/billing/webhooks/mercadopago')


def test_mercado_pago_cria_cobranca(mercado_pago):
    ConexaoFalsa.respostas = [(201, {'id': 'pref-1', 'init_point': 'https://prod', 'sandbox_init_point': 'https://sandbox'})]
    from decimal import Decimal
    cobranca = mercado_pago.criar_cobranca(1, Decimal('230.5'))
    assert cobranca == {'id': 'pref-1', 'link': 'https://sandbox'}
    metodo, caminho, corpo, cabecalhos = ConexaoFalsa.chamadas[0]
    assert (metodo, caminho) == ('POST', '/checkout/preferences')
    assert corpo['items'][0]['unit_price'] == 230.5
    assert corpo['external_reference'] == '1'
    assert corpo['notification_url'] == 'https://gateway/billing/webhooks/mercadopago'
    assert cabecalhos['Authorization'] == 'Bearer TEST-123'


def test_mercado_pago_producao_usa_init_point(monkeypatch):
    monkeypatch.setattr(infrastructure.http.client, 'HTTPSConnection', ConexaoFalsa)
    ConexaoFalsa.respostas = [(201, {'id': 'pref-2', 'init_point': 'https://prod'})]
    assert MercadoPagoGateway('APP_USR-1').criar_cobranca(2, 10)['link'] == 'https://prod'


def test_mercado_pago_consulta_e_estorna(mercado_pago):
    ConexaoFalsa.respostas = [
        (200, {'id': 555, 'status': 'approved', 'external_reference': '42'}),
        (200, {'id': 556, 'status': 'approved', 'external_reference': None}),
        (201, {'id': 1}),
    ]
    assert mercado_pago.consultar_pagamento('555') == {'id': '555', 'status': 'approved', 'id_os': 42}
    assert mercado_pago.consultar_pagamento('556')['id_os'] is None
    mercado_pago.estornar('555')
    assert ConexaoFalsa.chamadas[2][:2] == ('POST', '/v1/payments/555/refunds')
    assert ConexaoFalsa.chamadas[2][3]['X-Idempotency-Key'] == 'estorno-555'


def test_mercado_pago_com_erro(mercado_pago):
    ConexaoFalsa.respostas = [(500, {'message': 'erro'})]
    with pytest.raises(RuntimeError):
        mercado_pago.consultar_pagamento('1')


def test_assinatura_do_webhook(mercado_pago):
    manifesto = 'id:123;request-id:req-1;ts:1700000000;'
    assinatura = hmac.new(b'segredo', manifesto.encode(), hashlib.sha256).hexdigest()
    cabecalhos = {'x-signature': f'ts=1700000000,v1={assinatura}', 'x-request-id': 'req-1'}
    assert mercado_pago.notificacao_autentica(cabecalhos, '123')
    assert not mercado_pago.notificacao_autentica({**cabecalhos, 'x-request-id': 'outro'}, '123')
    assert not mercado_pago.notificacao_autentica({}, '123')
    assert MercadoPagoGateway('TEST-1').notificacao_autentica({}, '123')
