output "tabela_orcamentos" {
  value = aws_dynamodb_table.orcamentos.name
}

output "fila_comandos" {
  value = aws_sqs_queue.comandos.name
}

output "role_servico" {
  value = aws_iam_role.servico.arn
}
