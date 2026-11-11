from decimal import Decimal

TRANSICOES = {
    "Aguardando aprovação": ["Aguardando pagamento", "Recusado"],
    "Aguardando pagamento": ["Pago", "Cancelado"],
    "Pago": ["Estornado"],
    "Recusado": [],
    "Cancelado": [],
    "Estornado": [],
}

EVENTO_POR_STATUS = {
    "Aguardando aprovação": "OrcamentoGerado",
    "Aguardando pagamento": "OrcamentoAprovado",
    "Recusado": "OrcamentoRecusado",
    "Pago": "PagamentoAprovado",
    "Cancelado": "PagamentoRecusado",
    "Estornado": "PagamentoEstornado",
}


class DomainError(Exception):
    pass


class Orcamento:
    def __init__(self, id_os, id_cliente, itens, status="Aguardando aprovação"):
        try:
            self.id_os = int(id_os)
            self.id_cliente = int(id_cliente)
            self.itens = [
                {"descricao": str(item["descricao"]), "tipo": str(item["tipo"]), "valor": Decimal(str(item["valor"]))}
                for item in itens
            ]
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            raise DomainError("Dados do orçamento inválidos") from e
        if not self.itens or any(item["valor"] < 0 for item in self.itens):
            raise DomainError("O orçamento precisa de itens com valor não negativo")
        self.valor_total = sum(item["valor"] for item in self.itens)
        if self.valor_total <= 0:
            raise DomainError("O valor total do orçamento precisa ser maior que zero")
        self.status = status

    @staticmethod
    def pode_transicionar(status_atual, novo_status):
        return novo_status in TRANSICOES.get(status_atual, [])
