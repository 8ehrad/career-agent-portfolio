# Observation-only freshness. The existing hourly maintenance Lambda recovers due work.
resource "aws_sqs_queue" "freshness_dlq" {
  name                      = "${local.name}-freshness-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "freshness" {
  name                       = "${local.name}-freshness"
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 360
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.freshness_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_iam_role" "freshness" {
  name = "${local.name}-freshness"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_cloudwatch_log_group" "freshness" {
  name              = "/aws/lambda/${local.name}-freshness"
  retention_in_days = 30
}

resource "aws_iam_role_policy" "freshness" {
  role = aws_iam_role.freshness.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.freshness.arn}:*" },
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.freshness.arn },
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem"], Resource = aws_dynamodb_table.career.arn,
      Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = ["FRESHNESS#personal"] } } },
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.career.arn,
      Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = ["REED_HYDRATION_BUDGET#personal"] } } },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.integrations.arn }
    ]
  })
}

resource "aws_lambda_function" "freshness" {
  function_name    = "${local.name}-freshness"
  role             = aws_iam_role.freshness.arn
  handler          = "career_agent.handlers.freshness_worker.handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  memory_size      = 256
  timeout          = 60
  environment {
    variables = {
      TABLE_NAME                         = aws_dynamodb_table.career.name
      SOURCE_REGISTRY_PATH               = "/var/task/data/source_registry.json"
      INTEGRATION_SECRET_ARN             = aws_secretsmanager_secret.integrations.arn
      CAREER_CUSTOMER_ID                 = "personal"
      FRESHNESS_MODE                     = var.freshness_mode
      FRESHNESS_DAILY_REQUEST_LIMIT      = "100"
      REED_HYDRATION_DAILY_REQUEST_LIMIT = "2000"
    }
  }
  depends_on = [aws_iam_role_policy.freshness]
}

resource "aws_lambda_event_source_mapping" "freshness" {
  event_source_arn        = aws_sqs_queue.freshness.arn
  function_name           = aws_lambda_function.freshness.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
  scaling_config { maximum_concurrency = 2 }
}

resource "aws_cloudwatch_metric_alarm" "freshness_dlq" {
  alarm_name          = "${local.name}-freshness-dlq-visible"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.freshness_dlq.name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "freshness_errors" {
  alarm_name          = "${local.name}-freshness-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.freshness.function_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

variable "freshness_mode" {
  description = "Source freshness shadow monitoring. Off until an explicitly scoped rollout."
  type        = string
  default     = "off"
  validation {
    condition     = contains(["off", "observe"], var.freshness_mode)
    error_message = "Freshness supports off or observe only; enforcement is not implemented."
  }
}
