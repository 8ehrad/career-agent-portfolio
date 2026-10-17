locals {
  name                  = "${var.project_name}-${var.environment}"
  account_suffix        = data.aws_caller_identity.current.account_id
  artifact_bucket       = "${local.name}-artifacts-${local.account_suffix}"
  dashboard_bucket      = "${local.name}-dashboard-${local.account_suffix}"
  dashboard_origin      = "https://${aws_cloudfront_distribution.dashboard.domain_name}"
  dashboard_origins     = [local.dashboard_origin, var.dashboard_custom_origin]
  cognito_domain_prefix = "${local.name}-${local.account_suffix}"
  cognito_domain        = "https://${local.cognito_domain_prefix}.auth.${var.aws_region}.amazoncognito.com"
  rum_monitor_name      = "${local.name}-portfolio"
  rum_monitor_arn       = "arn:${data.aws_partition.current.partition}:rum:${var.aws_region}:${local.account_suffix}:appmonitor/${local.rum_monitor_name}"
  rum_resource_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowPublicTelemetryWrites"
      Effect    = "Allow"
      Principal = "*"
      Action    = "rum:PutRumEvents"
      Resource  = local.rum_monitor_arn
    }]
  })
}

resource "aws_dynamodb_table" "career" {
  name         = "${local.name}-data"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  attribute {
    name = "gsi1pk"
    type = "S"
  }

  attribute {
    name = "gsi1sk"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi1"
    projection_type = "ALL"

    key_schema {
      attribute_name = "gsi1pk"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "gsi1sk"
      key_type       = "RANGE"
    }
  }

  point_in_time_recovery {
    enabled = true
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }
}

resource "aws_s3_bucket" "artifacts" {
  bucket = local.artifact_bucket
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-raw-discovery"
    status = "Enabled"
    filter { prefix = "raw/" }
    expiration { days = 90 }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
}

resource "aws_s3_bucket" "dashboard" {
  bucket = local.dashboard_bucket
}

resource "aws_s3_bucket_server_side_encryption_configuration" "dashboard" {
  bucket = aws_s3_bucket.dashboard.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "dashboard" {
  bucket                  = aws_s3_bucket.dashboard.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_cloudfront_origin_access_control" "dashboard" {
  name                              = "${local.name}-dashboard"
  description                       = "Private S3 access for Career OS"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_response_headers_policy" "security" {
  name = "${local.name}-security-headers"

  security_headers_config {
    content_type_options { override = true }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
      preload                    = true
    }
    xss_protection {
      mode_block = true
      override   = true
      protection = true
    }
  }
}

resource "aws_cloudfront_distribution" "dashboard" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  comment             = "Career OS dashboard"

  origin {
    domain_name              = aws_s3_bucket.dashboard.bucket_regional_domain_name
    origin_id                = "dashboard-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.dashboard.id
  }

  default_cache_behavior {
    target_origin_id           = "dashboard-s3"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = "658327ea-f89d-4fab-a63d-7e88639e58f6"
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
  }

  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/404.html"
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/404.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
    # CloudFront reports TLSv1 for its managed default certificate. Setting
    # TLSv1.2 here creates perpetual state drift without changing the policy.
    minimum_protocol_version = "TLSv1"
  }
}

data "aws_iam_policy_document" "dashboard_bucket" {
  statement {
    sid       = "AllowCloudFrontRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.dashboard.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.dashboard.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "dashboard" {
  bucket = aws_s3_bucket.dashboard.id
  policy = data.aws_iam_policy_document.dashboard_bucket.json
}

resource "aws_cognito_user_pool_client" "dashboard" {
  provider                             = aws.untagged
  name                                 = "${local.name}-web"
  user_pool_id                         = var.cognito_user_pool_id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = [for origin in local.dashboard_origins : "${origin}/career"]
  logout_urls                          = [for origin in local.dashboard_origins : "${origin}/career"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  access_token_validity                = 1
  id_token_validity                    = 1
  refresh_token_validity               = 7

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}

resource "aws_cognito_user_pool_domain" "dashboard" {
  provider              = aws.untagged
  domain                = local.cognito_domain_prefix
  user_pool_id          = var.cognito_user_pool_id
  managed_login_version = 2

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cognito_managed_login_branding" "dashboard" {
  provider     = aws.untagged
  user_pool_id = var.cognito_user_pool_id
  client_id    = aws_cognito_user_pool_client.dashboard.id
  settings     = file("${path.module}/cognito-branding.json")

  depends_on = [aws_cognito_user_pool_domain.dashboard]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_rum_app_monitor" "portfolio" {
  name        = local.rum_monitor_name
  domain_list = [replace(var.dashboard_custom_origin, "https://", ""), aws_cloudfront_distribution.dashboard.domain_name]

  app_monitor_configuration {
    allow_cookies       = false
    enable_xray         = false
    session_sample_rate = 1
    telemetries         = ["errors", "performance", "http"]
  }

  cw_log_enabled = false
}

# aws_rum_app_monitor does not yet expose the resource-policy API. Keep the
# official CLI bridge tracked by a policy hash so changes are reapplied.
resource "terraform_data" "rum_resource_policy" {
  triggers_replace = [aws_rum_app_monitor.portfolio.arn, sha256(local.rum_resource_policy)]

  provisioner "local-exec" {
    command = "aws rum put-resource-policy --region ${var.aws_region} --name ${local.rum_monitor_name} --policy-document '${local.rum_resource_policy}'"
  }
}

resource "aws_secretsmanager_secret" "integrations" {
  name                    = "${local.name}/integrations"
  description             = "Approved job-source and analysis API credentials; update outside Terraform."
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "integrations" {
  secret_id     = aws_secretsmanager_secret.integrations.id
  secret_string = "{}"

  lifecycle { ignore_changes = [secret_string] }
}

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../build/lambda"
  output_path = "${path.module}/../build/career-agent.zip"
}

# A scoped API release must not republish the pipeline workers' artifact.
# The ordinary build script stages both directories for a full release.
data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/../build/api-lambda"
  output_path = "${path.module}/../build/career-agent-api.zip"
}

resource "aws_iam_role" "lambda" {
  name = "${local.name}-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${local.name}-runtime"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${local.account_suffix}:log-group:/aws/lambda/${local.name}-*:*"
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:DeleteItem",
          "dynamodb:BatchGetItem",
          "dynamodb:BatchWriteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:UpdateItem",
        ]
        Resource = [aws_dynamodb_table.career.arn, "${aws_dynamodb_table.career.arn}/index/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.artifacts.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.integrations.arn
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.alerts.arn
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:ReceiveMessage",
          "sqs:SendMessage",
        ]
        Resource = [
          aws_sqs_queue.workday.arn,
          aws_sqs_queue.pipeline_finalizer.arn,
          aws_sqs_queue.deep_analysis.arn,
          aws_sqs_queue.analysis_finalizer.arn,
          aws_sqs_queue.reed_hydration.arn,
          aws_sqs_queue.reed_hydration_ready.arn,
          aws_sqs_queue.freshness.arn,
          aws_sqs_queue.application_prepare.arn,
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:GetMetricData"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:DescribeAlarms"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["scheduler:GetSchedule"]
        Resource = "arn:${data.aws_partition.current.partition}:scheduler:${var.aws_region}:${local.account_suffix}:schedule/default/${local.name}-*"
      },
      {
        Effect = "Allow"
        Action = ["sqs:GetQueueAttributes"]
        Resource = [aws_sqs_queue.scheduler_dlq.arn, aws_sqs_queue.workday_dlq.arn,
          aws_sqs_queue.pipeline_finalizer_dlq.arn, aws_sqs_queue.deep_analysis_dlq.arn,
          aws_sqs_queue.analysis_finalizer_dlq.arn, aws_sqs_queue.reed_hydration_dlq.arn,
          aws_sqs_queue.reed_hydration_ready_dlq.arn, aws_sqs_queue.freshness_dlq.arn,
        aws_sqs_queue.application_prepare_dlq.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["rum:GetAppMonitorData"]
        Resource = local.rum_monitor_arn
      }
    ]
  })
}

locals {
  operations_environment = {
    OPERATIONS_RESOURCE_PREFIX = local.name
  }
  lambda_environment = {
    MATCHING_MODE                                = var.matching_mode
    APPLICATION_PROFILE_MODE                     = var.application_profile_mode
    APPLICATION_PREPARE_QUEUE_URL                = aws_sqs_queue.application_prepare.url
    FRESHNESS_MODE                               = var.freshness_mode
    FRESHNESS_QUEUE_URL                          = aws_sqs_queue.freshness.url
    TABLE_NAME                                   = aws_dynamodb_table.career.name
    ARTIFACT_BUCKET                              = aws_s3_bucket.artifacts.id
    PROFILE_PATH                                 = "/var/task/data/candidate_profile.example.json"
    SOURCE_REGISTRY_PATH                         = "/var/task/data/source_registry.json"
    EMPLOYER_UNIVERSE_PATH                       = "/var/task/data/employer_universe.json"
    EMPLOYER_SOURCE_ROUTES_PATH                  = "/var/task/data/employer_source_routes.json"
    INTEGRATION_SECRET_ARN                       = aws_secretsmanager_secret.integrations.arn
    STRONG_MODEL_ID                              = var.strong_model_id
    SES_SENDER                                   = var.owner_email
    DIGEST_TOPIC_ARN                             = aws_sns_topic.alerts.arn
    DASHBOARD_URL                                = var.dashboard_custom_origin
    ALLOWED_EMAIL                                = var.owner_email
    DEEP_ANALYSIS_LIMIT                          = "25"
    DIGEST_LIMIT                                 = "10"
    SALARY_RESEARCH_LIMIT                        = "20"
    RUM_APP_MONITOR_NAME                         = local.rum_monitor_name
    TWIN_ENVIRONMENT                             = var.twin_environment
    TWIN_LAMBDA_NAME                             = var.twin_lambda_name
    TWIN_API_ID                                  = var.twin_api_id
    CAREER_API_LAMBDA_NAME                       = "${local.name}-api"
    CAREER_SCHEDULED_LAMBDA_NAME                 = "${local.name}-scheduled"
    PORTFOLIO_DISTRIBUTION_ID                    = aws_cloudfront_distribution.dashboard.id
    WORKDAY_QUEUE_URL                            = aws_sqs_queue.workday.url
    PIPELINE_FINALIZER_QUEUE_URL                 = aws_sqs_queue.pipeline_finalizer.url
    WORKDAY_INGESTION_DEADLINE_MINUTES           = "45"
    ASYNC_DEEP_ANALYSIS_ENABLED                  = "true"
    DEEP_ANALYSIS_QUEUE_URL                      = aws_sqs_queue.deep_analysis.url
    ANALYSIS_FINALIZER_QUEUE_URL                 = aws_sqs_queue.analysis_finalizer.url
    REED_HYDRATION_QUEUE_URL                     = aws_sqs_queue.reed_hydration.url
    REED_HYDRATION_READY_QUEUE_URL               = aws_sqs_queue.reed_hydration_ready.url
    REED_HYDRATION_BUDGET_TIMEZONE               = "Europe/London"
    REED_HYDRATION_DAILY_REQUEST_LIMIT           = "2000"
    REED_HYDRATION_DISPATCH_BATCH_SIZE           = "500"
    REED_HYDRATION_MAX_ATTEMPTS                  = "5"
    REED_HYDRATION_MIN_REQUEST_INTERVAL_SECONDS  = "2"
    DEEP_ANALYSIS_DEADLINE_MINUTES               = "15"
    DEEP_ANALYSIS_MAXIMUM_CONCURRENCY            = "4"
    ANALYSIS_BACKLOG_STALE_DAYS                  = "14"
    CAREER_CUSTOMER_ID                           = "personal"
    ENVIRONMENT                                  = var.environment
    ANALYSIS_BUDGET_TIMEZONE                     = "Europe/London"
    ANALYSIS_DAILY_CALL_LIMIT                    = "75"
    ANALYSIS_DAILY_INPUT_TOKEN_LIMIT             = "225000"
    ANALYSIS_DAILY_OUTPUT_TOKEN_LIMIT            = "90000"
    ANALYSIS_DAILY_COST_LIMIT_MICRO_USD          = "1750000"
    ANALYSIS_ESTIMATED_OUTPUT_TOKENS             = "1000"
    ANALYSIS_INPUT_MICRO_USD_PER_MILLION_TOKENS  = "3000000"
    ANALYSIS_OUTPUT_MICRO_USD_PER_MILLION_TOKENS = "15000000"
    BACKLOG_DRAIN_MINIMUM_DAILY_CALLS            = "50"
    BACKLOG_DRAIN_MAXIMUM_DAILY_CALLS            = "75"
    BACKLOG_DRAIN_MAXIMUM_BATCH_SIZE             = "25"
    BACKLOG_DRAIN_OLDEST_JOB_SLO_HOURS           = "72"
    BACKLOG_DRAIN_THROTTLING_COOLDOWN_HOURS      = "6"
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.name}-api"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "scheduled" {
  name              = "/aws/lambda/${local.name}-scheduled"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "coverage_probe" {
  name              = "/aws/lambda/${local.name}-coverage-probe"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "workday_dispatcher" {
  name              = "/aws/lambda/${local.name}-workday-dispatcher"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "workday_worker" {
  name              = "/aws/lambda/${local.name}-workday-worker"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "deep_analysis_worker" {
  name              = "/aws/lambda/${local.name}-deep-analysis-worker"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "analysis_finalizer" {
  name              = "/aws/lambda/${local.name}-analysis-finalizer"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "backlog_drainer" {
  name              = "/aws/lambda/${local.name}-backlog-drainer"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "reed_hydration_worker" {
  name              = "/aws/lambda/${local.name}-reed-hydration-worker"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "reed_hydration_ready" {
  name              = "/aws/lambda/${local.name}-reed-hydration-ready"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "reed_hydration_drainer" {
  name              = "/aws/lambda/${local.name}-reed-hydration-drainer"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "application_prepare_worker" {
  name              = "/aws/lambda/${local.name}-application-prepare-worker"
  retention_in_days = 30
}

resource "aws_lambda_function" "api" {
  function_name    = "${local.name}-api"
  role             = aws_iam_role.lambda.arn
  handler          = "career_agent.handlers.api.handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  memory_size      = 768
  timeout          = 60

  environment { variables = merge(local.lambda_environment, local.operations_environment) }
  depends_on = [aws_cloudwatch_log_group.api, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function" "scheduled" {
  function_name    = "${local.name}-scheduled"
  role             = aws_iam_role.lambda.arn
  handler          = "career_agent.handlers.scheduled.handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  memory_size      = 1024
  timeout          = 900
  environment { variables = local.lambda_environment }
  depends_on = [aws_cloudwatch_log_group.scheduled, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function" "coverage_probe" {
  function_name    = "${local.name}-coverage-probe"
  role             = aws_iam_role.lambda.arn
  handler          = "career_agent.handlers.coverage_probe.handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  memory_size      = 768
  timeout          = 600
  environment { variables = local.lambda_environment }
  depends_on = [aws_cloudwatch_log_group.coverage_probe, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function" "workday_dispatcher" {
  function_name    = "${local.name}-workday-dispatcher"
  role             = aws_iam_role.lambda.arn
  handler          = "career_agent.handlers.workday_dispatcher.handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  memory_size      = 256
  timeout          = 60
  environment { variables = local.lambda_environment }
  depends_on = [aws_cloudwatch_log_group.workday_dispatcher, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function" "workday_worker" {
  function_name    = "${local.name}-workday-worker"
  role             = aws_iam_role.lambda.arn
  handler          = "career_agent.handlers.workday_worker.handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  memory_size      = 1024
  timeout          = 600
  environment { variables = local.lambda_environment }
  depends_on = [aws_cloudwatch_log_group.workday_worker, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function" "deep_analysis_worker" {
  function_name    = "${local.name}-deep-analysis-worker"
  role             = aws_iam_role.lambda.arn
  handler          = "career_agent.handlers.deep_analysis_worker.handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  memory_size      = 1024
  timeout          = 120
  environment { variables = local.lambda_environment }
  depends_on = [aws_cloudwatch_log_group.deep_analysis_worker, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function" "application_prepare_worker" {
  function_name    = "${local.name}-application-prepare-worker"
  role             = aws_iam_role.lambda.arn
  handler          = "career_agent.handlers.application_prepare_worker.handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  memory_size      = 1024
  timeout          = 420
  environment { variables = local.lambda_environment }
  depends_on = [aws_cloudwatch_log_group.application_prepare_worker, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function" "analysis_finalizer" {
  function_name    = "${local.name}-analysis-finalizer"
  role             = aws_iam_role.lambda.arn
  handler          = "career_agent.handlers.deep_analysis_finalize.handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  memory_size      = 1024
  timeout          = 300
  environment { variables = local.lambda_environment }
  depends_on = [aws_cloudwatch_log_group.analysis_finalizer, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function" "backlog_drainer" {
  function_name    = "${local.name}-backlog-drainer"
  role             = aws_iam_role.lambda.arn
  handler          = "career_agent.handlers.backlog_drainer.handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  memory_size      = 256
  timeout          = 60
  environment { variables = local.lambda_environment }
  depends_on = [aws_cloudwatch_log_group.backlog_drainer, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function" "reed_hydration_worker" {
  function_name    = "${local.name}-reed-hydration-worker"
  role             = aws_iam_role.lambda.arn
  handler          = "career_agent.handlers.reed_hydration_worker.handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  memory_size      = 256
  timeout          = 60
  environment { variables = local.lambda_environment }
  depends_on = [aws_cloudwatch_log_group.reed_hydration_worker, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function" "reed_hydration_ready" {
  function_name    = "${local.name}-reed-hydration-ready"
  role             = aws_iam_role.lambda.arn
  handler          = "career_agent.handlers.reed_hydration_ready.handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  memory_size      = 512
  timeout          = 60
  environment { variables = local.lambda_environment }
  depends_on = [aws_cloudwatch_log_group.reed_hydration_ready, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function" "reed_hydration_drainer" {
  function_name    = "${local.name}-reed-hydration-drainer"
  role             = aws_iam_role.lambda.arn
  handler          = "career_agent.handlers.reed_hydration_drainer.handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  memory_size      = 256
  timeout          = 300
  environment { variables = local.lambda_environment }
  depends_on = [aws_cloudwatch_log_group.reed_hydration_drainer, aws_iam_role_policy.lambda]
}

resource "aws_apigatewayv2_api" "dashboard" {
  name          = "${local.name}-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_credentials = false
    allow_headers     = ["authorization", "content-type"]
    allow_methods     = ["GET", "POST", "PATCH", "OPTIONS"]
    allow_origins     = local.dashboard_origins
    max_age           = 3600
  }
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.dashboard.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.dashboard.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cognito"
  jwt_configuration {
    audience = [aws_cognito_user_pool_client.dashboard.id]
    issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${var.cognito_user_pool_id}"
  }
}

resource "aws_apigatewayv2_route" "health" {
  api_id    = aws_apigatewayv2_api.dashboard.id
  route_key = "GET /health"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "preflight" {
  api_id    = aws_apigatewayv2_api.dashboard.id
  route_key = "OPTIONS /{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "authenticated" {
  api_id             = aws_apigatewayv2_api.dashboard.id
  route_key          = "ANY /{proxy+}"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.dashboard.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.dashboard.execution_arn}/*/*"
}

resource "aws_sqs_queue" "scheduler_dlq" {
  name                      = "${local.name}-scheduler-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "workday_dlq" {
  name                      = "${local.name}-workday-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "workday" {
  name                       = "${local.name}-workday"
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 900
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.workday_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "pipeline_finalizer_dlq" {
  name                      = "${local.name}-pipeline-finalizer-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "pipeline_finalizer" {
  name                       = "${local.name}-pipeline-finalizer"
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 1800
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.pipeline_finalizer_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "deep_analysis_dlq" {
  name                      = "${local.name}-deep-analysis-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "deep_analysis" {
  name                       = "${local.name}-deep-analysis"
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 300
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.deep_analysis_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "analysis_finalizer_dlq" {
  name                      = "${local.name}-analysis-finalizer-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "analysis_finalizer" {
  name                       = "${local.name}-analysis-finalizer"
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 1800
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.analysis_finalizer_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "application_prepare_dlq" {
  name                      = "${local.name}-application-prepare-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "application_prepare" {
  name                       = "${local.name}-application-prepare"
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 480
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.application_prepare_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "reed_hydration_dlq" {
  name                      = "${local.name}-reed-hydration-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "reed_hydration" {
  name                       = "${local.name}-reed-hydration"
  message_retention_seconds  = 1209600
  visibility_timeout_seconds = 120
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.reed_hydration_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "reed_hydration_ready_dlq" {
  name                      = "${local.name}-reed-hydration-ready-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "reed_hydration_ready" {
  name                       = "${local.name}-reed-hydration-ready"
  message_retention_seconds  = 1209600
  visibility_timeout_seconds = 120
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.reed_hydration_ready_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_lambda_event_source_mapping" "workday" {
  event_source_arn        = aws_sqs_queue.workday.arn
  function_name           = aws_lambda_function.workday_worker.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 4
  }
}

resource "aws_lambda_event_source_mapping" "pipeline_finalizer" {
  event_source_arn        = aws_sqs_queue.pipeline_finalizer.arn
  function_name           = aws_lambda_function.scheduled.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
}

resource "aws_lambda_event_source_mapping" "deep_analysis" {
  event_source_arn        = aws_sqs_queue.deep_analysis.arn
  function_name           = aws_lambda_function.deep_analysis_worker.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 4
  }
}

resource "aws_lambda_event_source_mapping" "application_prepare" {
  event_source_arn        = aws_sqs_queue.application_prepare.arn
  function_name           = aws_lambda_function.application_prepare_worker.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
  enabled                 = var.application_profile_mode == "rich"

  scaling_config {
    maximum_concurrency = 2
  }
}

resource "aws_lambda_event_source_mapping" "analysis_finalizer" {
  event_source_arn        = aws_sqs_queue.analysis_finalizer.arn
  function_name           = aws_lambda_function.analysis_finalizer.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
}

resource "aws_lambda_event_source_mapping" "reed_hydration" {
  event_source_arn        = aws_sqs_queue.reed_hydration.arn
  function_name           = aws_lambda_function.reed_hydration_worker.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 2
  }
}

resource "aws_lambda_event_source_mapping" "reed_hydration_ready" {
  event_source_arn        = aws_sqs_queue.reed_hydration_ready.arn
  function_name           = aws_lambda_function.reed_hydration_ready.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 4
  }
}

resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${local.name}-invoke"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = "lambda:InvokeFunction"
        Resource = [
          aws_lambda_function.workday_dispatcher.arn,
          aws_lambda_function.coverage_probe.arn,
          aws_lambda_function.backlog_drainer.arn,
          aws_lambda_function.reed_hydration_drainer.arn,
          aws_lambda_function.application_prepare_worker.arn,
        ]
      },
      { Effect = "Allow", Action = "sqs:SendMessage", Resource = aws_sqs_queue.scheduler_dlq.arn }
    ]
  })
}

resource "aws_scheduler_schedule" "pipeline" {
  name       = "${local.name}-every-two-days"
  state      = var.schedule_state
  group_name = "default"

  schedule_expression          = "rate(2 days)"
  schedule_expression_timezone = "Europe/London"

  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 30
  }

  target {
    arn      = aws_lambda_function.workday_dispatcher.arn
    role_arn = aws_iam_role.scheduler.arn
    # The scheduled time is stable across delivery retries, so it doubles as
    # the run id for DynamoDB's idempotency claim.
    # Do not use jsonencode here: Terraform escapes angle brackets as Unicode
    # sequences, which prevents EventBridge Scheduler from substituting its
    # context attributes before invoking Lambda.
    input = <<-JSON
      {
        "source": "eventbridge-scheduler",
        "run_id": "<aws.scheduler.scheduled-time>",
        "attempt_number": "<aws.scheduler.attempt-number>"
      }
    JSON
    dead_letter_config { arn = aws_sqs_queue.scheduler_dlq.arn }
    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 2
    }
  }
}

resource "aws_scheduler_schedule" "coverage_probe" {
  name       = "${local.name}-coverage-probe"
  state      = var.schedule_state
  group_name = "default"

  schedule_expression          = "rate(12 hours)"
  schedule_expression_timezone = "Europe/London"

  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 15
  }

  target {
    arn      = aws_lambda_function.coverage_probe.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "eventbridge-scheduler" })
    dead_letter_config { arn = aws_sqs_queue.scheduler_dlq.arn }
    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 2
    }
  }
}

resource "aws_scheduler_schedule" "backlog_drainer" {
  name       = "${local.name}-backlog-drainer"
  state      = var.schedule_state
  group_name = "default"

  schedule_expression          = "rate(6 hours)"
  schedule_expression_timezone = "Europe/London"

  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 15
  }

  target {
    arn      = aws_lambda_function.backlog_drainer.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = <<-JSON
      {
        "source": "eventbridge-scheduler",
        "run_id": "<aws.scheduler.scheduled-time>",
        "attempt_number": "<aws.scheduler.attempt-number>"
      }
    JSON
    dead_letter_config { arn = aws_sqs_queue.scheduler_dlq.arn }
    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 2
    }
  }
}

resource "aws_scheduler_schedule" "reed_hydration_drainer" {
  name       = "${local.name}-reed-hydration-drainer"
  state      = var.schedule_state
  group_name = "default"

  schedule_expression          = "rate(1 hour)"
  schedule_expression_timezone = "Europe/London"

  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 5
  }

  target {
    arn      = aws_lambda_function.reed_hydration_drainer.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "eventbridge-scheduler" })
    dead_letter_config { arn = aws_sqs_queue.scheduler_dlq.arn }
    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 2
    }
  }
}

resource "aws_scheduler_schedule" "application_prepare_reconciler" {
  name       = "${local.name}-application-prepare-reconciler"
  state      = var.application_profile_mode == "rich" && var.schedule_state == "ENABLED" ? "ENABLED" : "DISABLED"
  group_name = "default"

  schedule_expression          = "rate(15 minutes)"
  schedule_expression_timezone = "Europe/London"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.application_prepare_worker.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ kind = "application_reconcile" })
    dead_letter_config { arn = aws_sqs_queue.scheduler_dlq.arn }
    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 2
    }
  }
}

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.owner_email
}

resource "aws_cloudwatch_metric_alarm" "scheduled_errors" {
  alarm_name          = "${local.name}-scheduled-errors"
  alarm_description   = "Career pipeline Lambda has failed."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.scheduled.function_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "coverage_probe_errors" {
  alarm_name          = "${local.name}-coverage-probe-errors"
  alarm_description   = "Career coverage benchmark probe has failed."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.coverage_probe.function_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "workday_worker_errors" {
  alarm_name          = "${local.name}-workday-worker-errors"
  alarm_description   = "A Workday ingestion worker returned an unhandled error."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.workday_worker.function_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "workday_dispatcher_errors" {
  alarm_name          = "${local.name}-workday-dispatcher-errors"
  alarm_description   = "The Workday dispatcher returned an unhandled error."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.workday_dispatcher.function_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "deep_analysis_worker_errors" {
  alarm_name          = "${local.name}-deep-analysis-worker-errors"
  alarm_description   = "A deep-analysis worker returned an unhandled error."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.deep_analysis_worker.function_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "analysis_finalizer_errors" {
  alarm_name          = "${local.name}-analysis-finalizer-errors"
  alarm_description   = "The deep-analysis finalizer returned an unhandled error."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.analysis_finalizer.function_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "backlog_drainer_errors" {
  alarm_name          = "${local.name}-backlog-drainer-errors"
  alarm_description   = "The scheduled analysis backlog drainer returned an unhandled error."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.backlog_drainer.function_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "reed_hydration_worker_errors" {
  alarm_name          = "${local.name}-reed-hydration-worker-errors"
  alarm_description   = "A Reed description hydration worker returned an unhandled error."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.reed_hydration_worker.function_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "reed_hydration_ready_errors" {
  alarm_name          = "${local.name}-reed-hydration-ready-errors"
  alarm_description   = "A hydrated Reed advert could not enter the analysis backlog."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.reed_hydration_ready.function_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "reed_hydration_drainer_errors" {
  alarm_name          = "${local.name}-reed-hydration-drainer-errors"
  alarm_description   = "The Reed hydration backlog reconciler returned an unhandled error."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.reed_hydration_drainer.function_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "reed_hydration_failed_tasks" {
  alarm_name          = "${local.name}-reed-hydration-failed-tasks"
  alarm_description   = "At least one Reed advert exhausted its bounded hydration attempts."
  namespace           = "CareerAgent/ReedHydration"
  metric_name         = "Failed"
  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { Environment = var.environment }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "analysis_backlog_age" {
  alarm_name          = "${local.name}-analysis-backlog-age"
  alarm_description   = "The oldest eligible deep-analysis task has breached its 72-hour target."
  namespace           = "CareerAgent/Analysis"
  metric_name         = "BacklogOldestAgeHours"
  statistic           = "Maximum"
  period              = 21600
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 72
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { Environment = var.environment }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "workday_dlq_visible" {
  alarm_name          = "${local.name}-workday-dlq-visible"
  alarm_description   = "A Workday employer fetch exhausted its retries."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.workday_dlq.name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "pipeline_finalizer_dlq_visible" {
  alarm_name          = "${local.name}-pipeline-finalizer-dlq-visible"
  alarm_description   = "A pipeline finalization message exhausted its retries."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.pipeline_finalizer_dlq.name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "deep_analysis_dlq_visible" {
  alarm_name          = "${local.name}-deep-analysis-dlq-visible"
  alarm_description   = "A deep-analysis task exhausted its retries."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.deep_analysis_dlq.name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "analysis_finalizer_dlq_visible" {
  alarm_name          = "${local.name}-analysis-finalizer-dlq-visible"
  alarm_description   = "A deep-analysis finalization message exhausted its retries."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.analysis_finalizer_dlq.name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "application_prepare_dlq_visible" {
  alarm_name          = "${local.name}-application-prepare-dlq-visible"
  alarm_description   = "An application preparation task exhausted its retries."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.application_prepare_dlq.name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "reed_hydration_dlq_visible" {
  alarm_name          = "${local.name}-reed-hydration-dlq-visible"
  alarm_description   = "A Reed description hydration message exhausted its retries."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.reed_hydration_dlq.name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "reed_hydration_ready_dlq_visible" {
  alarm_name          = "${local.name}-reed-hydration-ready-dlq-visible"
  alarm_description   = "A hydrated Reed advert could not be admitted or rejected after retries."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.reed_hydration_ready_dlq.name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "dlq_visible" {
  alarm_name          = "${local.name}-dlq-visible"
  alarm_description   = "A scheduled pipeline invocation exhausted its retries."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.scheduler_dlq.name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "twin_lambda_errors" {
  alarm_name          = "${local.name}-twin-lambda-errors"
  alarm_description   = "The public twin Lambda returned an unhandled error."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = var.twin_lambda_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "twin_api_5xx" {
  alarm_name          = "${local.name}-twin-api-5xx"
  alarm_description   = "The public twin API returned a server error."
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { ApiId = var.twin_api_id }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "twin_chat_failures" {
  alarm_name          = "${local.name}-twin-chat-failures"
  alarm_description   = "The twin recorded a failed chat request."
  namespace           = "Twin/Runtime"
  metric_name         = "ChatFailures"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { Environment = var.twin_environment }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "portfolio_javascript_errors" {
  alarm_name          = "${local.name}-portfolio-javascript-errors"
  alarm_description   = "Public portfolio browsers recorded repeated JavaScript errors."
  namespace           = "AWS/RUM"
  metric_name         = "JsErrorCount"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { application_name = local.rum_monitor_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_budgets_budget" "monthly" {
  count = var.enable_aws_budget ? 1 : 0

  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.owner_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.owner_email]
  }
}
