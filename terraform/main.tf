terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      projeto  = "oficina"
      ambiente = var.env
    }
  }
}

locals {
  prefixo = "oficina-${var.env}"
}

data "aws_caller_identity" "atual" {}

data "aws_ssm_parameter" "cluster_name" {
  name = "/oficina/eks/cluster_name"
}

resource "aws_dynamodb_table" "orcamentos" {
  name         = "${local.prefixo}-orcamentos"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id_os"

  attribute {
    name = "id_os"
    type = "N"
  }
}

resource "aws_sqs_queue" "comandos_dlq" {
  name                      = "${local.prefixo}-billing-comandos-dlq.fifo"
  fifo_queue                = true
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "comandos" {
  name                        = "${local.prefixo}-billing-comandos.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  visibility_timeout_seconds  = 60

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.comandos_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_iam_role" "servico" {
  name = "${local.prefixo}-billing"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

resource "aws_iam_role_policy" "servico" {
  name = "acesso-billing"
  role = aws_iam_role.servico.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DescribeTable"]
        Resource = aws_dynamodb_table.orcamentos.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueUrl", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.comandos.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage", "sqs:GetQueueUrl"]
        Resource = "arn:aws:sqs:${var.region}:${data.aws_caller_identity.atual.account_id}:${local.prefixo}-os-eventos.fifo"
      }
    ]
  })
}

resource "aws_eks_pod_identity_association" "servico" {
  cluster_name    = data.aws_ssm_parameter.cluster_name.value
  namespace       = var.env
  service_account = "billing"
  role_arn        = aws_iam_role.servico.arn
}
