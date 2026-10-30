variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "environment" {
  type    = string
  default = "staging"
}

variable "domain_name" {
  type        = string
  description = "Public hostname, e.g. app.tenderpilot.example"
}

variable "acm_certificate_arn" {
  type        = string
  description = "ACM certificate for domain_name in the same region"
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "github_repository" {
  type        = string
  description = "owner/name allowed to deploy via OIDC"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "web_count" {
  type    = number
  default = 2
}

variable "worker_count" {
  type    = number
  default = 2
}

output "alb_dns_name" { value = aws_lb.web.dns_name }
output "ecr_repository" { value = aws_ecr_repository.app.repository_url }
output "documents_bucket" { value = aws_s3_bucket.docs.bucket }
output "deploy_role_arn" { value = aws_iam_role.deploy.arn }
output "ecs_cluster" { value = aws_ecs_cluster.main.name }
output "providers_secret" { value = aws_secretsmanager_secret.providers.name }
