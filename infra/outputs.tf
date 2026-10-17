output "api_url" {
  value = aws_apigatewayv2_api.dashboard.api_endpoint
}

output "dashboard_url" {
  value = local.dashboard_origin
}

output "dashboard_custom_url" {
  value = var.dashboard_custom_origin
}

output "dashboard_bucket" {
  value = aws_s3_bucket.dashboard.id
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.dashboard.id
}

output "cognito_client_id" {
  value = aws_cognito_user_pool_client.dashboard.id
}

output "cognito_user_pool_id" {
  value = var.cognito_user_pool_id
}

output "cognito_domain" {
  value = local.cognito_domain
}

output "cognito_redirect_uri" {
  value = "${local.dashboard_origin}/career"
}

output "integration_secret_arn" {
  value = aws_secretsmanager_secret.integrations.arn
}

output "scheduler_state" {
  value = aws_scheduler_schedule.pipeline.state
}

output "rum_app_monitor_id" {
  value = aws_rum_app_monitor.portfolio.app_monitor_id
}

output "rum_app_monitor_name" {
  value = local.rum_monitor_name
}

output "rum_region" {
  value = var.aws_region
}
