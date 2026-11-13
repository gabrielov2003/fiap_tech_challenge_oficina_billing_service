variable "region" {
  description = "Região AWS"
  type        = string
  default     = "us-east-1"
}

variable "env" {
  description = "Ambiente do deploy: dev (branch dev) ou prod (branch main)"
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "O ambiente deve ser dev ou prod."
  }
}
