# ---------------- container platform ----------------
resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
}

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [aws_secretsmanager_secret.app.arn, aws_secretsmanager_secret.providers.arn] }]
  })
}

# Task role: only what the application itself needs (least privilege).
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "task" {
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "${aws_s3_bucket.docs.arn}/org/*" },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.docs.arn, Condition = { StringLike = { "s3:prefix" = ["org/*"] } } },
      { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.docs.arn },
      { Effect = "Allow", Action = ["textract:DetectDocumentText", "textract:AnalyzeDocument"], Resource = "*" },
    ]
  })
}

locals {
  env = [
    { name = "TP_ENVIRONMENT", value = var.environment },
    { name = "TP_MODE", value = "integrated" },
    { name = "TP_BASE_URL", value = "https://${var.domain_name}" },
    { name = "TP_STORAGE_BACKEND", value = "s3" },
    { name = "TP_S3_BUCKET", value = aws_s3_bucket.docs.bucket },
    { name = "TP_S3_REGION", value = var.region },
    { name = "TP_S3_KMS_KEY_ID", value = aws_kms_key.docs.arn },
    { name = "TP_TEXTRACT_ENABLED", value = "true" },
    { name = "TP_COOKIE_SECURE", value = "true" },
    { name = "TP_LOG_JSON", value = "true" },
  ]
  secrets = [for k in ["TP_SECRET_KEY", "TP_DATABASE_URL", "TP_REDIS_URL"] : { name = k, valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::" }]
  image   = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
  services = {
    web    = { command = null, cpu = 1024, memory = 2048, count = var.web_count }
    worker = { command = ["celery", "-A", "app.workers.celery_app", "worker", "-Q", "default,documents,ai,notifications", "--concurrency", "4"], cpu = 2048, memory = 4096, count = var.worker_count }
    beat   = { command = ["celery", "-A", "app.workers.celery_app", "beat", "--schedule", "/tmp/celerybeat-schedule"], cpu = 256, memory = 512, count = 1 }
  }
}

resource "aws_ecs_task_definition" "svc" {
  for_each                 = merge(local.services, { migrate = { command = ["alembic", "upgrade", "head"], cpu = 512, memory = 1024, count = 0 } })
  family                   = "tenderpilot-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([merge({
    name         = "app"
    image        = local.image
    essential    = true
    environment  = local.env
    secrets      = local.secrets
    portMappings = each.key == "web" ? [{ containerPort = 8080 }] : []
    logConfiguration = {
      logDriver = "awslogs"
      options   = { awslogs-group = aws_cloudwatch_log_group.app.name, awslogs-region = var.region, awslogs-stream-prefix = each.key }
    }
  }, each.value.command == null ? {} : { command = each.value.command })])
}

resource "aws_ecs_service" "svc" {
  for_each        = local.services
  name            = "tenderpilot-${each.key}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.svc[each.key].arn
  desired_count   = each.value.count
  launch_type     = "FARGATE"
  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }
  dynamic "load_balancer" {
    for_each = each.key == "web" ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.web.arn
      container_name   = "app"
      container_port   = 8080
    }
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  lifecycle { ignore_changes = [desired_count] }
}

resource "aws_appautoscaling_target" "web" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.svc["web"].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.web_count
  max_capacity       = var.web_count * 4
}

resource "aws_appautoscaling_policy" "web_cpu" {
  name               = "${local.name}-web-cpu"
  service_namespace  = "ecs"
  resource_id        = aws_appautoscaling_target.web.resource_id
  scalable_dimension = aws_appautoscaling_target.web.scalable_dimension
  policy_type        = "TargetTrackingScaling"
  target_tracking_scaling_policy_configuration {
    target_value = 60
    predefined_metric_specification { predefined_metric_type = "ECSServiceAverageCPUUtilization" }
  }
}

# ---------------- ingress ----------------
resource "aws_lb" "web" {
  name               = local.name
  load_balancer_type = "application"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]
  idle_timeout       = 300 # NiceGUI uses long-lived websockets
}

resource "aws_lb_target_group" "web" {
  name        = local.name
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"
  stickiness {
    type    = "lb_cookie"
    enabled = true # NiceGUI client state is per-process
  }
  health_check {
    path    = "/healthz"
    matcher = "200"
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.web.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.web.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# ---------------- CI deploy role (GitHub OIDC) ----------------
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "deploy" {
  name = "${local.name}-github-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:${var.environment}" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "deploy" {
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload"], Resource = aws_ecr_repository.app.arn },
      { Effect = "Allow", Action = ["ecs:UpdateService", "ecs:RunTask", "ecs:DescribeServices"], Resource = "*" },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.execution.arn, aws_iam_role.task.arn] },
    ]
  })
}
