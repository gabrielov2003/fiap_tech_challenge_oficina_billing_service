import logging

from domain import Orcamento, DomainError, EVENTO_POR_STATUS
from ports import OrcamentoRepositoryPort, PagamentoPort, MensageriaPort, MetricasPort

logger = logging.getLogger('billing.aplicacao')

orcamento_repo: OrcamentoRepositoryPort = None
pagamentos: PagamentoPort = None
mensageria: MensageriaPort = None
metricas: MetricasPort = None
filas = {'comandos': None, 'eventos_os': None}


class MensagemInvalida(Exception):
    pass


class IntegracaoIndisponivel(Exception):
    pass


def publicar_evento(tipo, id_os, dados, correlation_id=None):
    mensageria.enviar(filas['eventos_os'], {
        'tipo': tipo, 'id_os': id_os, 'dados': dados, 'correlation_id': correlation_id
    })
    logger.info('evento_publicado', extra={'tipo': tipo, 'id_os': id_os, 'correlation_id': correlation_id})


class BillingService:
    @staticmethod
    def buscar_orcamento(id_os):
        return orcamento_repo.buscar(id_os)

    @staticmethod
    def gerar_orcamento(id_os, dados, correlation_id=None):
        try:
            orcamento = Orcamento(id_os, dados.get('id_cliente'), dados.get('itens') or [])
        except DomainError as e:
            logger.warning('orcamento_falhou', extra={'id_os': id_os, 'motivo': str(e), 'correlation_id': correlation_id})
            metricas.orcamento_alterado('Falhou')
            publicar_evento('OrcamentoFalhou', id_os, {'motivo': str(e)}, correlation_id)
            return
        if not orcamento_repo.criar(orcamento):
            BillingService._reemitir(orcamento_repo.buscar(id_os), correlation_id)
            return
        metricas.orcamento_alterado(orcamento.status)
        logger.info('orcamento_gerado', extra={
            'id_os': orcamento.id_os, 'valor_total': orcamento.valor_total, 'correlation_id': correlation_id
        })
        publicar_evento('OrcamentoGerado', orcamento.id_os, {
            'status': orcamento.status, 'valor_total': orcamento.valor_total
        }, correlation_id)

    @staticmethod
    def aprovar(orcamento, aprovado, correlation_id=None):
        novo_status = "Aguardando pagamento" if aprovado else "Recusado"
        if orcamento['status'] == novo_status:
            BillingService._reemitir(orcamento, correlation_id)
            return orcamento
        if not Orcamento.pode_transicionar(orcamento['status'], novo_status):
            return {"erro": f"O orçamento não está aguardando aprovação (status atual: '{orcamento['status']}')"}
        campos = {}
        if aprovado:
            try:
                cobranca = pagamentos.criar_cobranca(orcamento['id_os'], orcamento['valor_total'])
            except Exception as e:
                logger.exception('falha_mercado_pago', extra={'id_os': orcamento['id_os'], 'operacao': 'criar_cobranca'})
                metricas.erro_integracao('mercadopago', 'criar_cobranca')
                raise IntegracaoIndisponivel("Não foi possível gerar o pagamento, tente novamente") from e
            campos = {'cobranca_id': cobranca['id'], 'link_pagamento': cobranca['link']}
        return BillingService._transicionar(orcamento, novo_status, campos, correlation_id)

    @staticmethod
    def processar_pagamento(id_pagamento, correlation_id=None):
        pagamento = pagamentos.consultar_pagamento(id_pagamento)
        orcamento = orcamento_repo.buscar(pagamento['id_os']) if pagamento['id_os'] else None
        if not orcamento:
            logger.warning('pagamento_sem_orcamento', extra={'id_pagamento': id_pagamento})
            return None
        if pagamento['status'] == 'approved':
            return BillingService._pagamento_aprovado(orcamento, pagamento, correlation_id)
        if pagamento['status'] in ('rejected', 'cancelled') and orcamento['status'] == "Aguardando pagamento":
            return BillingService._transicionar(orcamento, "Cancelado", {'pagamento_id': pagamento['id']}, correlation_id)
        return orcamento

    @staticmethod
    def _pagamento_aprovado(orcamento, pagamento, correlation_id):
        if orcamento['status'] == "Aguardando pagamento":
            return BillingService._transicionar(orcamento, "Pago", {'pagamento_id': pagamento['id']}, correlation_id)
        if orcamento.get('pagamento_id') == pagamento['id']:
            if orcamento['status'] == "Pago":
                BillingService._reemitir(orcamento, correlation_id)
            return orcamento
        pagamentos.estornar(pagamento['id'])
        logger.warning('pagamento_fora_do_fluxo_estornado', extra={
            'id_os': orcamento['id_os'], 'id_pagamento': pagamento['id'], 'status_orcamento': orcamento['status']
        })
        metricas.orcamento_alterado('Estorno automatico')
        return orcamento

    @staticmethod
    def estornar_pagamento(id_os, dados, correlation_id=None):
        orcamento = orcamento_repo.buscar(id_os)
        if not orcamento:
            raise MensagemInvalida(f"Orçamento da OS {id_os} não encontrado")
        if orcamento['status'] == "Estornado":
            BillingService._reemitir(orcamento, correlation_id)
            return
        if orcamento['status'] != "Pago":
            logger.warning('estorno_ignorado', extra={'id_os': id_os, 'status_atual': orcamento['status']})
            return
        pagamentos.estornar(orcamento['pagamento_id'])
        BillingService._transicionar(orcamento, "Estornado", {'motivo_estorno': dados.get('motivo')}, correlation_id)

    @staticmethod
    def notificacao_autentica(cabecalhos, id_recurso):
        return pagamentos.notificacao_autentica(cabecalhos, id_recurso)

    @staticmethod
    def banco_disponivel():
        try:
            return orcamento_repo.ping()
        except Exception:
            logger.exception('banco_indisponivel')
            return False

    @staticmethod
    def _transicionar(orcamento, novo_status, campos, correlation_id):
        if not orcamento_repo.atualizar_status(orcamento['id_os'], orcamento['status'], novo_status, campos):
            return {"erro": "O orçamento foi alterado por outra operação, tente novamente"}
        atualizado = {**orcamento, **campos, 'status': novo_status}
        metricas.orcamento_alterado(novo_status)
        logger.info('orcamento_alterado', extra={
            'id_os': orcamento['id_os'], 'status_anterior': orcamento['status'], 'novo_status': novo_status,
            'correlation_id': correlation_id
        })
        BillingService._reemitir(atualizado, correlation_id)
        return atualizado

    @staticmethod
    def _reemitir(orcamento, correlation_id):
        publicar_evento(EVENTO_POR_STATUS[orcamento['status']], orcamento['id_os'], {
            'status': orcamento['status'], 'valor_total': orcamento['valor_total']
        }, correlation_id)


COMANDOS = {
    'GerarOrcamento': BillingService.gerar_orcamento,
    'EstornarPagamento': BillingService.estornar_pagamento,
}


def tratar_comando(mensagem):
    acao = COMANDOS.get(mensagem.get('tipo'))
    if not acao or not mensagem.get('id_os'):
        raise MensagemInvalida(f"Comando inválido: {mensagem.get('tipo')}")
    acao(mensagem['id_os'], mensagem.get('dados') or {}, mensagem.get('correlation_id'))


def processar_comandos():
    for mensagem, recibo in mensageria.receber(filas['comandos']):
        contexto = {
            'tipo': mensagem.get('tipo'), 'id_os': mensagem.get('id_os'), 'correlation_id': mensagem.get('correlation_id')
        }
        try:
            tratar_comando(mensagem)
        except MensagemInvalida:
            logger.warning('mensagem_invalida', extra=contexto)
            metricas.erro_integracao('sqs', 'mensagem_invalida')
        except Exception:
            logger.exception('falha_processamento_mensagem', extra=contexto)
            metricas.erro_integracao('sqs', 'falha_processamento')
            continue
        mensageria.confirmar(filas['comandos'], recibo)
