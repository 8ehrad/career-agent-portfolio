variable "aws_region" {
  description = "AWS region for the career agent."
  type        = string
  default     = "eu-west-2"
}

variable "matching_mode" {
  description = "Rich matching requires its pinned profile/index and compatible analysis tasks."
  type        = string
  default     = "legacy"
  validation {
    condition     = contains(["legacy", "rich"], var.matching_mode)
    error_message = "Matching mode must be legacy or rich."
  }
}

variable "application_profile_mode" {
  description = "Use the rich asynchronous application writer only after its release checks pass."
  type        = string
  default     = "legacy"
  validation {
    condition     = contains(["legacy", "rich"], var.application_profile_mode)
    error_message = "Application profile mode must be legacy or rich."
  }
}

variable "project_name" {
  description = "Resource name prefix."
  type        = string
  default     = "example-career-agent"
}

variable "environment" {
  description = "Deployment environment."
  type        = string
  default     = "prod"
}

variable "owner_email" {
  description = "Only email allowed to access the dashboard and receive digests."
  type        = string
  # Supply an explicit value for a new, separately authorized deployment.
}

variable "dashboard_custom_origin" {
  description = "Canonical public origin for Career OS links and OAuth callbacks."
  type        = string
  # Supply an explicit value for a new, separately authorized deployment.
}

variable "cognito_user_pool_id" {
  description = "Existing Career OS user pool. The deployment role cannot read Cognito MFA configuration, so dependants are managed separately."
  type        = string
  # Supply an explicit value for a new, separately authorized deployment.
}

variable "strong_model_id" {
  description = "Bedrock inference profile used for deep analysis and application drafting."
  type        = string
  # Sonnet 5 is listed in eu-west-2 but is not enabled for this account.
  # Sonnet 4.6 is the newest strong model verified with a live Converse call.
  default = "eu.anthropic.claude-sonnet-4-6"
}

variable "schedule_state" {
  description = "Historical recurring pipeline template; disabled by default."
  type        = string
  default     = "DISABLED"

  validation {
    condition     = contains(["ENABLED", "DISABLED"], var.schedule_state)
    error_message = "schedule_state must be ENABLED or DISABLED."
  }
}

variable "monthly_budget_usd" {
  description = "Monthly AWS cost alert threshold."
  type        = number
  default     = 25
}

variable "enable_aws_budget" {
  description = "Create an AWS Budget when the deployment principal has budgets:ModifyBudget."
  type        = bool
  default     = false
}

variable "twin_environment" {
  description = "Environment dimension emitted by the twin runtime."
  type        = string
  default     = "dev"
}

variable "twin_lambda_name" {
  description = "Deployed twin API Lambda function name."
  type        = string
  # Supply an explicit value for a new, separately authorized deployment.
}

variable "twin_api_id" {
  description = "HTTP API ID serving the twin."
  type        = string
  # Supply an explicit value for a new, separately authorized deployment.
}
