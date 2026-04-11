locals {
  predictions_image_name     = "${local.name_s3}-predicciones"
  predictions_container_name = "predicciones"
  predictions_schedule_name  = "${local.name}-predicciones-hourly"
}

resource "aws_ecr_repository" "predicciones" {
  name                 = local.predictions_image_name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  force_delete = true
  tags         = local.common_tags
}

resource "aws_ecr_lifecycle_policy" "predicciones" {
  repository = aws_ecr_repository.predicciones.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 10 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "ecs_predicciones" {
  name              = "/ecs/${local.name}-predicciones"
  retention_in_days = 7
  tags              = local.common_tags
}

resource "aws_security_group" "ecs_predicciones" {
  name   = "${local.name}-ecs-predicciones-sg"
  vpc_id = var.vpc_id
  tags   = local.common_tags

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "rds_ingress_from_ecs_predicciones" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.ecs_predicciones.id
  security_group_id        = aws_security_group.rds.id
  description              = "PostgreSQL access from ECS predictions task"
}

resource "aws_ecs_cluster" "predicciones" {
  name = "${local.name}-predicciones"
  tags = local.common_tags
}

resource "aws_iam_role" "ecs_task_execution_predicciones" {
  name = "${local.name}-ecs-predicciones-execution"
  tags = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_predicciones" {
  role       = aws_iam_role.ecs_task_execution_predicciones.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_secretsmanager_secret" "predicciones_rds_password" {
  name                    = "${local.name}-predicciones-rds-password"
  recovery_window_in_days = 0
  tags                    = local.common_tags
}

resource "aws_secretsmanager_secret_version" "predicciones_rds_password" {
  secret_id     = aws_secretsmanager_secret.predicciones_rds_password.id
  secret_string = var.db_password
}

resource "aws_iam_role_policy" "ecs_task_execution_predicciones_secrets" {
  name = "${local.name}-ecs-predicciones-execution-secrets"
  role = aws_iam_role.ecs_task_execution_predicciones.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [
          aws_secretsmanager_secret.predicciones_rds_password.arn
        ]
      }
    ]
  })
}

resource "aws_iam_role" "ecs_events_predicciones" {
  name = "${local.name}-ecs-predicciones-events"
  tags = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "ecs_events_predicciones" {
  name = "${local.name}-ecs-predicciones-events-policy"
  role = aws_iam_role.ecs_events_predicciones.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecs:RunTask"
        ]
        Resource = [
          aws_ecs_task_definition.predicciones.arn
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "iam:PassRole"
        ]
        Resource = [
          aws_iam_role.ecs_task_execution_predicciones.arn
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:DescribeTasks",
          "ecs:StopTask"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_ecs_task_definition" "predicciones" {
  family                   = "${local.name}-predicciones"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "2048"
  memory                   = "8192"

  execution_role_arn = aws_iam_role.ecs_task_execution_predicciones.arn

  container_definitions = jsonencode([
    {
      name      = local.predictions_container_name
      image     = "${aws_ecr_repository.predicciones.repository_url}:latest"
      essential = true

      environment = [
        { name = "RDS_HOST", value = aws_db_instance.postgres.address },
        { name = "RDS_DB", value = aws_db_instance.postgres.db_name },
        { name = "RDS_USER", value = aws_db_instance.postgres.username },
        { name = "RDS_PORT", value = tostring(aws_db_instance.postgres.port) },
        { name = "PYTHONIOENCODING", value = "utf-8" }
      ]

      secrets = [
        {
          name      = "RDS_PASSWORD"
          valueFrom = aws_secretsmanager_secret.predicciones_rds_password.arn
        }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs_predicciones.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  tags = local.common_tags
}

resource "aws_scheduler_schedule" "predicciones_hourly" {
  name                         = local.predictions_schedule_name
  description                  = "Run prediction container every hour at minute 5"
  schedule_expression          = "cron(15 * * * ? *)"
  schedule_expression_timezone = "America/Guayaquil"
  state                        = "ENABLED"
  group_name                   = "default"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_ecs_cluster.predicciones.arn
    role_arn = aws_iam_role.ecs_events_predicciones.arn

    ecs_parameters {
      launch_type         = "FARGATE"
      task_count          = 1
      task_definition_arn = aws_ecs_task_definition.predicciones.arn
      platform_version    = "LATEST"

      network_configuration {
        subnets          = var.subnet_ecs
        security_groups  = [aws_security_group.ecs_predicciones.id]
        assign_public_ip = true
      }
    }
  }
}
