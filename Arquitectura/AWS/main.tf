provider "aws" {
  region = var.region
}

data "archive_file" "lambda_zip" {
  depends_on = [terraform_data.lambda_package]

  type        = "zip"
  source_dir  = "${path.module}/.lambda_package"
  output_path = "${path.module}/lambda_function.zip"
}

data "aws_ami" "ubuntu_2204" {
  most_recent = true
  owners      = ["099720109477"]

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

locals {
  name                        = "${var.project}-${var.env}"
  name_s3                     = lower(replace(local.name, "_", "-"))
  name_iot                    = replace("${var.project}_${var.env}", "-", "_")
  iot_topic                   = "energia/${var.id_cliente}/${var.id_dispositivo}/metricas"
  iot_client_id               = coalesce(var.mqtt_client_id, var.id_dispositivo)
  manage_iot_certificate      = var.iot_certificate_arn == null
  iot_principal_arn           = local.manage_iot_certificate ? aws_iot_certificate.device[0].arn : var.iot_certificate_arn
  glue_script_key             = "scripts/glue/zc_energia_clientes_mediciones.py"
  glue_script_migracion_key   = "scripts/glue/zc_energia_clientes_mediciones_migracion.py"
  glue_script_horaria_key     = "scripts/glue/zc_energia_clientes_horaria.py"
  glue_script_extremos_key    = "scripts/glue/zc_energia_clientes_extremos.py"
  raw_prefix                  = "raw"
  logs_prefix                 = "logs"
  curated_prefix              = "curada/energia/clientes"
  curated_horaria_prefix      = "curada/energia/clientes_horaria"
  curated_extremos_prefix     = "curada/energia/clientes_extremos"
  curated_table_name          = "zc_energia_clientes_mediciones"
  curated_horaria_table_name  = "zc_energia_clientes_horaria"
  curated_extremos_table_name = "zc_energia_clientes_extremos"
  common_tags = {
    proyecto = "uoc_tfm"
  }
}

resource "terraform_data" "lambda_package" {
  triggers_replace = [
    filesha256("${path.module}/lambda_function.py"),
    filesha256("${path.module}/lambda_rds_ingest.py"),
    filesha256("${path.module}/lambda_agg_rds_modelo.py"),
    filesha256("${path.module}/cleanup_rds_lambda.py"),
    filesha256("${path.module}/requirements-lambda.txt"),
  ]

  provisioner "local-exec" {
    interpreter = ["PowerShell", "-Command"]
    command     = <<-EOT
      Remove-Item -Recurse -Force "${path.module}/.lambda_package" -ErrorAction SilentlyContinue
      New-Item -ItemType Directory -Force -Path "${path.module}/.lambda_package" | Out-Null
      Copy-Item "${path.module}/lambda_function.py" "${path.module}/.lambda_package/lambda_function.py" -Force
      Copy-Item "${path.module}/lambda_rds_ingest.py" "${path.module}/.lambda_package/lambda_rds_ingest.py" -Force
      Copy-Item "${path.module}/lambda_agg_rds_modelo.py" "${path.module}/.lambda_package/lambda_agg_rds_modelo.py" -Force
      Copy-Item "${path.module}/cleanup_rds_lambda.py" "${path.module}/.lambda_package/cleanup_rds_lambda.py" -Force
      python -m pip install --upgrade --target "${path.module}/.lambda_package" --requirement "${path.module}/requirements-lambda.txt" --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 --only-binary=:all:
    EOT
  }
}

resource "aws_s3_bucket" "raw" {
  bucket        = local.name_s3
  force_destroy = true
  tags          = local.common_tags
}

resource "aws_s3_bucket" "athena_results" {
  bucket        = "${local.name_s3}-athena-results"
  force_destroy = true
  tags          = local.common_tags
}

resource "aws_s3_bucket_public_access_block" "raw" {
  bucket = aws_s3_bucket.raw.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "athena_results" {
  bucket = aws_s3_bucket.athena_results.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "raw" {
  bucket = aws_s3_bucket.raw.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_versioning" "athena_results" {
  bucket = aws_s3_bucket.athena_results.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_glue_catalog_database" "iot" {
  name = "${lower(local.name_iot)}_db"
  tags = local.common_tags
}

resource "aws_athena_workgroup" "wg" {
  name = "${local.name}-wg"
  tags = local.common_tags

  configuration {
    result_configuration {
      output_location = "s3://${aws_s3_bucket.athena_results.bucket}/results/"
    }
  }
}

resource "aws_db_subnet_group" "db" {
  name       = "${local.name}-subnets"
  subnet_ids = var.subnet_ids
  tags       = local.common_tags
}

resource "aws_security_group" "rds" {
  name   = "${local.name}-rds-sg"
  vpc_id = var.vpc_id
  tags   = local.common_tags

  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.web.id]
  }

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.lambda.id]
  }

  ingress {
    description = "PostgreSQL access from personal public IP"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["186.4.251.197/32"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_instance" "postgres" {
  identifier = "${local.name}-db"

  engine         = "postgres"
  instance_class = "db.t4g.micro"

  allocated_storage = 30
  db_name           = "iotdb"
  username          = "iotadmin"
  password          = var.db_password

  db_subnet_group_name   = aws_db_subnet_group.db.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = true
  tags                   = local.common_tags

  skip_final_snapshot = true
}

resource "aws_security_group" "web" {
  name   = "${local.name}-web-sg"
  vpc_id = var.vpc_id
  tags   = local.common_tags

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = var.web_ssh_cidr_blocks
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "web" {
  ami                         = data.aws_ami.ubuntu_2204.id
  instance_type               = var.web_instance_type
  subnet_id                   = var.public_subnet_id
  vpc_security_group_ids      = [aws_security_group.web.id]
  associate_public_ip_address = true
  key_name                    = var.web_key_name

  user_data = <<-EOF
    #!/bin/bash
    apt update -y
    apt upgrade -y
  EOF

  tags = merge(local.common_tags, {
    Name = "${local.name}-web"
  })
}

resource "aws_eip" "web" {
  domain = "vpc"
  tags = merge(local.common_tags, {
    Name = "${local.name}-web-eip"
  })
}

resource "aws_eip_association" "web" {
  instance_id   = aws_instance.web.id
  allocation_id = aws_eip.web.id
}

resource "aws_iam_role" "lambda" {
  name = "${local.name}-lambda-role"
  tags = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_cloudwatch_log_group" "lambda_iot_to_rds" {
  name              = "/aws/lambda/${local.name}_iot_to_rds"
  retention_in_days = 7
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "lambda_iot_to_s3" {
  name              = "/aws/lambda/${local.name}_iot_to_s3"
  retention_in_days = 7
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "lambda_rds_cleanup" {
  name              = "/aws/lambda/${local.name}_rds_cleanup"
  retention_in_days = 7
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "lambda_rds_agg_modelo" {
  name              = "/aws/lambda/${local.name}_rds_agg_modelo"
  retention_in_days = 7
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "glue_mediciones_continuous" {
  name              = "/aws-glue/jobs/mediciones"
  retention_in_days = 7
  tags              = local.common_tags
}

resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "lambda_vpc_execution" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_cloudwatch_log_group" "iot_logs_v2" {
  name              = "/aws/iot/LogsV2"
  retention_in_days = 7
  tags              = local.common_tags
}

resource "aws_iam_role" "iot_logs" {
  name = "${local.name}-iot-logs-role"
  tags = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "iot.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "iot_logs" {
  name = "${local.name}_iot_logs_policy"
  role = aws_iam_role.iot_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iot_logging_options" "rules_engine" {
  default_log_level = "INFO"
  role_arn          = aws_iam_role.iot_logs.arn

  depends_on = [
    aws_iam_role_policy.iot_logs,
    aws_cloudwatch_log_group.iot_logs_v2,
  ]
}

resource "aws_security_group" "lambda" {
  name   = "${local.name}-lambda-sg"
  vpc_id = var.vpc_id
  tags   = local.common_tags

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_iam_role_policy" "lambda_raw_s3_access" {
  name = "${local.name}_lambda_raw_s3_access"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
        ]
        Resource = [
          "${aws_s3_bucket.raw.arn}/${local.raw_prefix}/*",
        ]
      }
    ]
  })
}

resource "aws_lambda_function" "iot_to_rds" {
  function_name = "${local.name}_iot_to_s3"
  tags          = local.common_tags

  runtime     = "python3.12"
  handler     = "lambda_function.lambda_handler"
  timeout     = 30
  memory_size = 256

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  role = aws_iam_role.lambda.arn

  depends_on = [
    aws_cloudwatch_log_group.lambda_iot_to_s3,
    aws_iam_role_policy_attachment.lambda_basic_execution,
    aws_iam_role_policy.lambda_raw_s3_access,
  ]

  environment {
    variables = {
      RAW_BUCKET = aws_s3_bucket.raw.bucket
      RAW_PREFIX = local.raw_prefix
    }
  }
}

resource "aws_lambda_function" "iot_to_rds_writer" {
  function_name = "${local.name}_iot_to_rds"
  tags          = local.common_tags

  runtime     = "python3.12"
  handler     = "lambda_rds_ingest.lambda_handler"
  timeout     = 30
  memory_size = 256

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  role = aws_iam_role.lambda.arn

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_iot_to_rds,
    aws_iam_role_policy_attachment.lambda_basic_execution,
    aws_iam_role_policy_attachment.lambda_vpc_execution,
  ]

  environment {
    variables = {
      DB_HOST = aws_db_instance.postgres.address
      DB_NAME = "iotdb"
      DB_USER = "iotadmin"
      DB_PASS = var.db_password
    }
  }
}

resource "aws_lambda_function" "rds_cleanup" {
  function_name = "${local.name}_rds_cleanup"
  tags          = local.common_tags

  runtime     = "python3.12"
  handler     = "cleanup_rds_lambda.lambda_handler"
  timeout     = 30
  memory_size = 256

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  role = aws_iam_role.lambda.arn

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_rds_cleanup,
    aws_iam_role_policy_attachment.lambda_basic_execution,
    aws_iam_role_policy_attachment.lambda_vpc_execution,
  ]

  environment {
    variables = {
      DB_HOST = aws_db_instance.postgres.address
      DB_NAME = "iotdb"
      DB_USER = "iotadmin"
      DB_PASS = var.db_password
    }
  }
}

resource "aws_lambda_function" "rds_agg_modelo" {
  function_name = "${local.name}_rds_agg_modelo"
  tags          = local.common_tags

  runtime     = "python3.12"
  handler     = "lambda_agg_rds_modelo.lambda_handler"
  timeout     = 30
  memory_size = 256

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  role = aws_iam_role.lambda.arn

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_rds_agg_modelo,
    aws_iam_role_policy_attachment.lambda_basic_execution,
    aws_iam_role_policy_attachment.lambda_vpc_execution,
  ]

  environment {
    variables = {
      DB_HOST = aws_db_instance.postgres.address
      DB_NAME = "iotdb"
      DB_USER = "iotadmin"
      DB_PASS = var.db_password
    }
  }
}

resource "aws_cloudwatch_event_rule" "rds_cleanup_daily" {
  name                = "${local.name}-rds-cleanup-daily"
  description         = "Run RDS cleanup daily at 2 AM UTC"
  schedule_expression = "cron(0 2 * * ? *)"
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "rds_cleanup_daily" {
  rule      = aws_cloudwatch_event_rule.rds_cleanup_daily.name
  target_id = "invoke-rds-cleanup-lambda"
  arn       = aws_lambda_function.rds_cleanup.arn
}

resource "aws_lambda_permission" "allow_eventbridge_invoke_rds_cleanup" {
  statement_id  = "AllowExecutionFromEventBridgeRdsCleanup"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.rds_cleanup.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.rds_cleanup_daily.arn
}

resource "aws_cloudwatch_event_rule" "rds_agg_modelo" {
  name                = "${local.name}-rds-agg-10m"
  description         = "Run 10-minute RDS aggregation at minute 5,15,25,..."
  schedule_expression = "cron(5/10 * * * ? *)"
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "rds_agg_modelo" {
  rule      = aws_cloudwatch_event_rule.rds_agg_modelo.name
  target_id = "invoke-rds-agg-modelo-lambda"
  arn       = aws_lambda_function.rds_agg_modelo.arn
}

resource "aws_lambda_permission" "allow_eventbridge_invoke_rds_agg_modelo" {
  statement_id  = "AllowExecutionFromEventBridgeRdsAgg5m"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.rds_agg_modelo.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.rds_agg_modelo.arn
}

resource "aws_iot_thing" "device" {
  name = var.id_dispositivo
}

resource "aws_iot_certificate" "device" {
  count = local.manage_iot_certificate ? 1 : 0

  active = true
}

resource "aws_iot_policy" "device_publish" {
  name = "${local.name_iot}_device_publish"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["iot:Connect"]
        Resource = [
          "arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:client/${local.iot_client_id}"
        ]
      },
      {
        Effect = "Allow"
        Action = ["iot:Publish"]
        Resource = [
          "arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:topic/${local.iot_topic}"
        ]
      }
    ]
  })
}

resource "aws_iot_policy_attachment" "device_publish" {
  policy = aws_iot_policy.device_publish.name
  target = local.iot_principal_arn
}

resource "aws_iot_thing_principal_attachment" "device_cert" {
  thing     = aws_iot_thing.device.name
  principal = local.iot_principal_arn
}

resource "local_sensitive_file" "iot_certificate_pem" {
  count = local.manage_iot_certificate ? 1 : 0

  content  = aws_iot_certificate.device[0].certificate_pem
  filename = "${path.module}/certs/${var.id_dispositivo}.certificate.pem"
}

resource "local_sensitive_file" "iot_private_key" {
  count = local.manage_iot_certificate ? 1 : 0

  content  = aws_iot_certificate.device[0].private_key
  filename = "${path.module}/certs/${var.id_dispositivo}.private.key"
}

resource "local_sensitive_file" "iot_public_key" {
  count = local.manage_iot_certificate ? 1 : 0

  content  = aws_iot_certificate.device[0].public_key
  filename = "${path.module}/certs/${var.id_dispositivo}.public.key"
}

resource "aws_iot_topic_rule" "to_s3" {
  name    = "${local.name_iot}_rule_iot_to_s3"
  enabled = true

  sql         = "SELECT *, topic() as mqtt_topic, timestamp() as ts FROM '${local.iot_topic}'"
  sql_version = "2016-03-23"

  lambda {
    function_arn = aws_lambda_function.iot_to_rds.arn
  }

  tags = local.common_tags
}

resource "aws_iot_topic_rule" "to_rds" {
  name    = "${local.name_iot}_rule_iot_to_rds"
  enabled = true

  sql         = "SELECT *, topic() as mqtt_topic, timestamp() as ts FROM '${local.iot_topic}'"
  sql_version = "2016-03-23"

  lambda {
    function_arn = aws_lambda_function.iot_to_rds_writer.arn
  }

  tags = local.common_tags
}

resource "aws_lambda_permission" "allow_iot_invoke_s3" {
  statement_id  = "AllowExecutionFromIoTRuleS3"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.iot_to_rds.function_name
  principal     = "iot.amazonaws.com"
  source_arn    = aws_iot_topic_rule.to_s3.arn
}

resource "aws_lambda_permission" "allow_iot_invoke_rds" {
  statement_id  = "AllowExecutionFromIoTRuleRds"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.iot_to_rds_writer.function_name
  principal     = "iot.amazonaws.com"
  source_arn    = aws_iot_topic_rule.to_rds.arn
}

resource "aws_iam_role" "glue" {
  name = "${local.name}-glue-role"
  tags = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "glue.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "glue_service" {
  role       = aws_iam_role.glue.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

resource "aws_iam_role_policy" "glue_s3_access" {
  name = "${local.name}_glue_s3_access"
  role = aws_iam_role.glue.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
        ]
        Resource = [
          aws_s3_bucket.raw.arn,
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = [
          "${aws_s3_bucket.raw.arn}/*",
        ]
      }
    ]
  })
}

resource "aws_iam_role" "glue_notebook" {
  name = "${local.name}-glue-notebook-role"
  tags = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "glue.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "glue_notebook_service" {
  role       = aws_iam_role.glue_notebook.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

resource "aws_iam_role_policy" "glue_notebook_access" {
  name = "${local.name}_glue_notebook_access"
  role = aws_iam_role.glue_notebook.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
        ]
        Resource = [
          aws_s3_bucket.raw.arn,
          aws_s3_bucket.athena_results.arn,
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = [
          "${aws_s3_bucket.raw.arn}/*",
          "${aws_s3_bucket.athena_results.arn}/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "glue:GetDatabase",
          "glue:GetDatabases",
          "glue:GetTable",
          "glue:GetTables",
          "glue:GetPartitions",
          "glue:CreateTable",
          "glue:UpdateTable",
          "glue:DeleteTable",
          "glue:BatchCreatePartition",
          "glue:BatchDeletePartition",
          "glue:BatchUpdatePartition",
          "glue:CreatePartition",
          "glue:UpdatePartition",
          "glue:DeletePartition"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "iam:PassRole"
        ]
        Resource = aws_iam_role.glue_notebook.arn
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "glue.amazonaws.com"
          }
        }
      }
    ]
  })
}

resource "aws_s3_object" "glue_script" {
  bucket = aws_s3_bucket.raw.id
  key    = local.glue_script_key
  source = "${path.module}/glue_etl_zc_energia_clientes_mediciones.py"
  etag   = filemd5("${path.module}/glue_etl_zc_energia_clientes_mediciones.py")
}

resource "aws_s3_object" "glue_script_migracion" {
  bucket = aws_s3_bucket.raw.id
  key    = local.glue_script_migracion_key
  source = "${path.module}/glue_etl_migracion_zc_energia_clientes_mediciones.py"
  etag   = filemd5("${path.module}/glue_etl_migracion_zc_energia_clientes_mediciones.py")
}

resource "aws_s3_object" "glue_script_horaria" {
  bucket = aws_s3_bucket.raw.id
  key    = local.glue_script_horaria_key
  source = "${path.module}/glue_elt_zc_energia_clientes_horaria.py"
  etag   = filemd5("${path.module}/glue_elt_zc_energia_clientes_horaria.py")
}

resource "aws_s3_object" "glue_script_extremos" {
  bucket = aws_s3_bucket.raw.id
  key    = local.glue_script_extremos_key
  source = "${path.module}/glue_elt_zc_energia_clientes_extremos.py"
  etag   = filemd5("${path.module}/glue_elt_zc_energia_clientes_extremos.py")
}

resource "aws_s3_object" "logs_prefix" {
  bucket  = aws_s3_bucket.raw.id
  key     = "${local.logs_prefix}/"
  content = ""
}

resource "aws_glue_job" "zc_energia_clientes_mediciones" {
  name     = "${local.name}-zc-energia-clientes-mediciones"
  role_arn = aws_iam_role.glue.arn
  tags     = local.common_tags

  command {
    name            = "glueetl"
    script_location = "s3://${aws_s3_bucket.raw.bucket}/${aws_s3_object.glue_script.key}"
    python_version  = "3"
  }

  glue_version      = "5.0"
  max_retries       = 1
  timeout           = 30
  worker_type       = "G.1X"
  number_of_workers = 2

  default_arguments = {
    "--RAW_BUCKET"                       = aws_s3_bucket.raw.bucket
    "--RAW_PREFIX"                       = local.raw_prefix
    "--CURATED_PREFIX"                   = local.curated_prefix
    "--job-bookmark-option"              = "job-bookmark-enable"
    "--enable-continuous-cloudwatch-log" = "true"
    "--enable-continuous-log-filter"     = "true"
    "--continuous-log-logGroup"          = aws_cloudwatch_log_group.glue_mediciones_continuous.name
    "--enable-metrics"                   = "true"
    "--enable-observability-metrics"     = "true"
    "--enable-glue-datacatalog"          = "true"
    "--TempDir"                          = "s3://${aws_s3_bucket.raw.bucket}/tmp/glue/"
  }

  depends_on = [
    aws_iam_role_policy_attachment.glue_service,
    aws_iam_role_policy.glue_s3_access,
    aws_cloudwatch_log_group.glue_mediciones_continuous,
    aws_s3_object.glue_script,
  ]
}

resource "aws_glue_job" "zc_energia_clientes_mediciones_migracion" {
  name     = "${local.name}-zc-energia-clientes-mediciones-migracion"
  role_arn = aws_iam_role.glue.arn
  tags     = local.common_tags

  command {
    name            = "glueetl"
    script_location = "s3://${aws_s3_bucket.raw.bucket}/${aws_s3_object.glue_script_migracion.key}"
    python_version  = "3"
  }

  glue_version      = "4.0"
  max_retries       = 0
  timeout           = 60
  worker_type       = "G.1X"
  number_of_workers = 2

  default_arguments = {
    "--RAW_BUCKET"                       = aws_s3_bucket.raw.bucket
    "--RAW_PREFIX"                       = local.raw_prefix
    "--CURATED_PREFIX"                   = local.curated_prefix
    "--job-bookmark-option"              = "job-bookmark-disable"
    "--enable-continuous-cloudwatch-log" = "true"
    "--enable-continuous-log-filter"     = "true"
    "--enable-metrics"                   = "true"
    "--enable-observability-metrics"     = "true"
    "--enable-glue-datacatalog"          = "true"
    "--TempDir"                          = "s3://${aws_s3_bucket.raw.bucket}/tmp/glue/"
  }

  # On-demand migration job: no trigger resource attached.
  depends_on = [
    aws_iam_role_policy_attachment.glue_service,
    aws_iam_role_policy.glue_s3_access,
    aws_s3_object.glue_script_migracion,
  ]
}

resource "aws_glue_trigger" "zc_energia_clientes_mediciones_hourly" {
  name              = "${local.name}-zc-energia-clientes-mediciones-hourly"
  type              = "SCHEDULED"
  schedule          = "cron(0 7 * * ? *)"
  start_on_creation = true
  tags              = local.common_tags

  actions {
    job_name = aws_glue_job.zc_energia_clientes_mediciones.name
  }
}

resource "aws_glue_job" "zc_energia_clientes_horaria" {
  name     = "${local.name}-zc-energia-clientes-horaria"
  role_arn = aws_iam_role.glue.arn
  tags     = local.common_tags

  command {
    name            = "glueetl"
    script_location = "s3://${aws_s3_bucket.raw.bucket}/${aws_s3_object.glue_script_horaria.key}"
    python_version  = "3"
  }

  glue_version      = "5.0"
  max_retries       = 1
  timeout           = 30
  worker_type       = "G.1X"
  number_of_workers = 2

  default_arguments = {
    "--RAW_BUCKET"                       = aws_s3_bucket.raw.bucket
    "--SOURCE_DATABASE"                  = aws_glue_catalog_database.iot.name
    "--SOURCE_TABLE"                     = aws_glue_catalog_table.zc_energia_clientes_mediciones.name
    "--TARGET_CURATED_PREFIX"            = local.curated_horaria_prefix
    "--FECHA_CORTE"                      = "YYYY-MM-DD"
    "--job-bookmark-option"              = "job-bookmark-disable"
    "--enable-continuous-cloudwatch-log" = "false"
    "--enable-metrics"                   = "true"
    "--enable-observability-metrics"     = "true"
    "--enable-glue-datacatalog"          = "true"
    "--TempDir"                          = "s3://${aws_s3_bucket.raw.bucket}/tmp/glue/"
  }

  depends_on = [
    aws_iam_role_policy_attachment.glue_service,
    aws_iam_role_policy.glue_s3_access,
    aws_s3_object.glue_script_horaria,
    aws_glue_catalog_table.zc_energia_clientes_mediciones,
  ]
}

resource "aws_glue_trigger" "zc_energia_clientes_horaria_daily" {
  name              = "${local.name}-zc-energia-clientes-horaria-daily"
  type              = "SCHEDULED"
  schedule          = "cron(10 6 * * ? *)"
  start_on_creation = true
  tags              = local.common_tags

  actions {
    job_name = aws_glue_job.zc_energia_clientes_horaria.name
  }
}

resource "aws_glue_job" "zc_energia_clientes_extremos" {
  name     = "${local.name}-zc-energia-clientes-extremos"
  role_arn = aws_iam_role.glue.arn
  tags     = local.common_tags

  command {
    name            = "glueetl"
    script_location = "s3://${aws_s3_bucket.raw.bucket}/${aws_s3_object.glue_script_extremos.key}"
    python_version  = "3"
  }

  glue_version      = "5.0"
  max_retries       = 1
  timeout           = 30
  worker_type       = "G.1X"
  number_of_workers = 2

  default_arguments = {
    "--RAW_BUCKET"                       = aws_s3_bucket.raw.bucket
    "--SOURCE_DATABASE"                  = aws_glue_catalog_database.iot.name
    "--SOURCE_TABLE"                     = aws_glue_catalog_table.zc_energia_clientes_mediciones.name
    "--TARGET_CURATED_PREFIX"            = local.curated_extremos_prefix
    "--FECHA_CORTE"                      = "YYYY-MM-DD"
    "--job-bookmark-option"              = "job-bookmark-disable"
    "--enable-continuous-cloudwatch-log" = "false"
    "--enable-metrics"                   = "true"
    "--enable-observability-metrics"     = "true"
    "--enable-glue-datacatalog"          = "true"
    "--TempDir"                          = "s3://${aws_s3_bucket.raw.bucket}/tmp/glue/"
  }

  depends_on = [
    aws_iam_role_policy_attachment.glue_service,
    aws_iam_role_policy.glue_s3_access,
    aws_s3_object.glue_script_extremos,
    aws_glue_catalog_table.zc_energia_clientes_mediciones,
  ]
}

resource "aws_glue_trigger" "zc_energia_clientes_extremos_daily" {
  name              = "${local.name}-zc-energia-clientes-extremos-daily"
  type              = "SCHEDULED"
  schedule          = "cron(0 7 * * ? *)"
  start_on_creation = true
  tags              = local.common_tags

  actions {
    job_name = aws_glue_job.zc_energia_clientes_extremos.name
  }
}

resource "aws_glue_catalog_table" "zc_energia_clientes_mediciones" {
  name          = local.curated_table_name
  database_name = aws_glue_catalog_database.iot.name
  table_type    = "EXTERNAL_TABLE"
  parameters = {
    "EXTERNAL"                  = "TRUE"
    "classification"            = "parquet"
    "parquet.compression"       = "snappy"
    "typeOfData"                = "file"
    "projection.enabled"        = "false"
    "projection.periodo.type"   = "date"
    "projection.periodo.format" = "yyyy-MM-dd"
    "projection.periodo.range"  = "2020-01-01,NOW"
    "projection.hora.type"      = "integer"
    "projection.hora.range"     = "0,23"
    "storage.location.template" = "s3://${aws_s3_bucket.raw.bucket}/${local.curated_prefix}/periodo=$${periodo}/hora=$${hora}/"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.raw.bucket}/${local.curated_prefix}/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      name                  = "parquet"
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "cliente"
      type = "string"
    }

    columns {
      name = "dispositivo"
      type = "string"
    }

    columns {
      name = "fase"
      type = "int"
    }

    columns {
      name = "voltaje"
      type = "double"
    }

    columns {
      name = "corriente"
      type = "double"
    }

    columns {
      name = "potencia"
      type = "double"
    }

    columns {
      name = "factor_potencia"
      type = "double"
    }

    columns {
      name = "frecuencia"
      type = "double"
    }

    columns {
      name = "energia_activa"
      type = "double"
    }

    columns {
      name = "fecha_medicion"
      type = "timestamp"
    }

    columns {
      name = "event_ts"
      type = "timestamp"
    }

    columns {
      name = "fecha_ingesta"
      type = "timestamp"
    }

    columns {
      name = "mqtt_topic"
      type = "string"
    }

    columns {
      name = "ts_raw"
      type = "bigint"
    }
  }

  partition_keys {
    name = "periodo"
    type = "date"
  }

  partition_keys {
    name = "hora"
    type = "int"
  }

  depends_on = [
    aws_glue_job.zc_energia_clientes_mediciones,
  ]
}

resource "aws_glue_catalog_table" "zc_energia_clientes_horaria" {
  name          = local.curated_horaria_table_name
  database_name = aws_glue_catalog_database.iot.name
  table_type    = "EXTERNAL_TABLE"
  parameters = {
    "EXTERNAL"                  = "TRUE"
    "classification"            = "parquet"
    "parquet.compression"       = "snappy"
    "typeOfData"                = "file"
    "projection.enabled"        = "false"
    "projection.periodo.type"   = "date"
    "projection.periodo.format" = "yyyy-MM-dd"
    "projection.periodo.range"  = "2020-01-01,NOW"
    "storage.location.template" = "s3://${aws_s3_bucket.raw.bucket}/${local.curated_horaria_prefix}/periodo=$${periodo}/"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.raw.bucket}/${local.curated_horaria_prefix}/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      name                  = "parquet"
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "cliente"
      type = "string"
    }

    columns {
      name = "dispositivo"
      type = "string"
    }

    columns {
      name = "fase"
      type = "int"
    }

    columns {
      name = "hora"
      type = "timestamp"
    }

    columns {
      name = "voltaje_promedio"
      type = "double"
    }

    columns {
      name = "corriente_promedio"
      type = "double"
    }

    columns {
      name = "potencia_promedio"
      type = "double"
    }

    columns {
      name = "factor_potencia_promedio"
      type = "double"
    }

    columns {
      name = "frecuencia_promedio"
      type = "double"
    }

    columns {
      name = "energia_activa_promedio"
      type = "double"
    }

    columns {
      name = "fecha_ingesta"
      type = "timestamp"
    }

    columns {
      name = "muestras"
      type = "bigint"
    }
  }

  partition_keys {
    name = "periodo"
    type = "date"
  }

  depends_on = [
    aws_glue_job.zc_energia_clientes_horaria,
  ]
}

resource "aws_glue_catalog_table" "zc_energia_clientes_extremos" {
  name          = local.curated_extremos_table_name
  database_name = aws_glue_catalog_database.iot.name
  table_type    = "EXTERNAL_TABLE"
  parameters = {
    "EXTERNAL"                  = "TRUE"
    "classification"            = "parquet"
    "parquet.compression"       = "snappy"
    "typeOfData"                = "file"
    "projection.enabled"        = "false"
    "projection.periodo.type"   = "date"
    "projection.periodo.format" = "yyyy-MM-dd"
    "projection.periodo.range"  = "2020-01-01,NOW"
    "storage.location.template" = "s3://${aws_s3_bucket.raw.bucket}/${local.curated_extremos_prefix}/periodo=$${periodo}/"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.raw.bucket}/${local.curated_extremos_prefix}/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      name                  = "parquet"
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "cliente"
      type = "string"
    }

    columns {
      name = "dispositivo"
      type = "string"
    }

    columns {
      name = "fase"
      type = "int"
    }

    columns {
      name = "voltaje_minimo"
      type = "double"
    }

    columns {
      name = "voltaje_maximo"
      type = "double"
    }

    columns {
      name = "corriente_minima"
      type = "double"
    }

    columns {
      name = "corriente_maxima"
      type = "double"
    }

    columns {
      name = "potencia_minima"
      type = "double"
    }

    columns {
      name = "potencia_maxima"
      type = "double"
    }

    columns {
      name = "factor_potencia_minimo"
      type = "double"
    }

    columns {
      name = "factor_potencia_maximo"
      type = "double"
    }

    columns {
      name = "frecuencia_minima"
      type = "double"
    }

    columns {
      name = "frecuencia_maxima"
      type = "double"
    }

    columns {
      name = "energia_activa_minima"
      type = "double"
    }

    columns {
      name = "energia_activa_maxima"
      type = "double"
    }

    columns {
      name = "fecha_ingesta"
      type = "timestamp"
    }

    columns {
      name = "muestras"
      type = "bigint"
    }
  }

  partition_keys {
    name = "periodo"
    type = "date"
  }

  depends_on = [
    aws_glue_job.zc_energia_clientes_extremos,
  ]
}

resource "aws_glue_crawler" "zc_energia_clientes_mediciones_daily" {
  name          = "${local.name}-crawler-zc-energia-clientes-mediciones-daily"
  database_name = aws_glue_catalog_database.iot.name
  role          = aws_iam_role.glue.arn
  schedule      = "cron(30 6 * * ? *)"
  tags          = local.common_tags

  catalog_target {
    database_name = aws_glue_catalog_database.iot.name
    tables        = [aws_glue_catalog_table.zc_energia_clientes_mediciones.name]
  }

  schema_change_policy {
    delete_behavior = "LOG"
    update_behavior = "UPDATE_IN_DATABASE"
  }

  recrawl_policy {
    recrawl_behavior = "CRAWL_EVERYTHING"
  }

  depends_on = [
    aws_iam_role_policy_attachment.glue_service,
    aws_iam_role_policy.glue_s3_access,
    aws_glue_catalog_table.zc_energia_clientes_mediciones,
  ]
}

resource "aws_glue_crawler" "zc_energia_clientes_horaria_daily" {
  name          = "${local.name}-crawler-zc-energia-clientes-horaria-daily"
  database_name = aws_glue_catalog_database.iot.name
  role          = aws_iam_role.glue.arn
  schedule      = "cron(0 7 * * ? *)"
  tags          = local.common_tags

  catalog_target {
    database_name = aws_glue_catalog_database.iot.name
    tables        = [aws_glue_catalog_table.zc_energia_clientes_horaria.name]
  }

  schema_change_policy {
    delete_behavior = "LOG"
    update_behavior = "UPDATE_IN_DATABASE"
  }

  recrawl_policy {
    recrawl_behavior = "CRAWL_EVERYTHING"
  }

  depends_on = [
    aws_iam_role_policy_attachment.glue_service,
    aws_iam_role_policy.glue_s3_access,
    aws_glue_catalog_table.zc_energia_clientes_horaria,
  ]
}

resource "aws_glue_crawler" "zc_energia_clientes_extremos_daily" {
  name          = "${local.name}-crawler-zc-energia-clientes-extremos-daily"
  database_name = aws_glue_catalog_database.iot.name
  role          = aws_iam_role.glue.arn
  schedule      = "cron(0 7 * * ? *)"
  tags          = local.common_tags

  catalog_target {
    database_name = aws_glue_catalog_database.iot.name
    tables        = [aws_glue_catalog_table.zc_energia_clientes_extremos.name]
  }

  schema_change_policy {
    delete_behavior = "LOG"
    update_behavior = "UPDATE_IN_DATABASE"
  }

  recrawl_policy {
    recrawl_behavior = "CRAWL_EVERYTHING"
  }

  depends_on = [
    aws_iam_role_policy_attachment.glue_service,
    aws_iam_role_policy.glue_s3_access,
    aws_glue_catalog_table.zc_energia_clientes_extremos,
  ]
}

data "aws_caller_identity" "current" {}
