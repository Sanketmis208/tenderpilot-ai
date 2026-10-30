# Deployment (AWS)

Reference infrastructure lives in `infra/aws` (Terraform). **It has not been applied** as part of this repository's
verification; run `terraform plan` in your account and review before applying.

## What it provisions

VPC (2 public + 2 private subnets, NAT, S3 gateway endpoint) · ALB with HTTPS (TLS 1.3 policy) and HTTP→HTTPS redirect,
sticky sessions and 300 s idle timeout for NiceGUI websockets · ECS Fargate services `web`, `worker`, `beat` and a
`migrate` task definition · RDS PostgreSQL 16 (encrypted, forced SSL, backups, Multi-AZ + deletion protection in
production) · ElastiCache Redis (encryption at rest/in transit) · private, versioned, SSE-KMS S3 bucket with TLS-only
policy and lifecycle rules · Secrets Manager (`<env>/app` generated, `<env>/providers` for API keys you add) · CloudWatch
logs + Container Insights · ECR (immutable tags, scan on push) · least-privilege task role (S3 `org/*` prefix, KMS,
Textract) · GitHub OIDC deploy role scoped to one repository environment · CPU target-tracking autoscaling for `web`.

## Steps

1. Request an ACM certificate for your domain in the target region.
2. `cd infra/aws && terraform init && terraform apply -var domain_name=app.example.com -var acm_certificate_arn=… -var github_repository=owner/tenderpilot-ai`
3. Add provider secrets to `<env>/providers` (e.g. `TP_ANTHROPIC_API_KEY`, SMTP) and reference them in the task definition `secrets` list.
4. Enable pgvector once: `CREATE EXTENSION vector; CREATE EXTENSION pg_trgm;` (the initial migration also attempts this; the RDS master user can create them).
5. In GitHub: set `secrets.AWS_ROLE_ARN` (output `deploy_role_arn`) and vars `AWS_REGION`, `ECR_REPOSITORY`, `ECS_CLUSTER`, `ECS_NETWORK_CONFIG`; run the **Deploy** workflow (builds, pushes, migrates, rolls services).
6. Point DNS at `alb_dns_name`. Create the first platform admin with `tenderpilot create-admin` (ECS exec or one-off task).

## Operations

* Health: ALB uses `/healthz`; `/readyz` reports DB/Redis/storage/LLM/OCR state.
* Logs are structured JSON (`TP_LOG_JSON=true`) with `request_id` correlation.
* Scale workers independently; expensive parsing/AI runs on the `documents` and `ai` queues.
* Backups: RDS automated backups (14 days) + S3 versioning. Test restores regularly (`aws rds restore-db-instance-to-point-in-time`).
* Stuck jobs are re-dispatched every 10 minutes by `beat` (`tp.scheduled.requeue_stuck`).

## External resources to provision

AWS account + region with Textract · domain + ACM certificate · Anthropic API key (integrated mode) · embeddings
endpoint (optional) · SMTP provider (e.g. SES) · error tracking (Sentry or CloudWatch alarms; not wired in code yet).
