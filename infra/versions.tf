terraform {
  required_version = ">= 1.8.0"

  required_providers {
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Application = var.project_name
      Environment = var.environment
      ManagedBy   = "Terraform"
      Owner       = "Example Candidate"
    }
  }
}

# This account's deployment role can create Cognito and SES resources but cannot
# tag them. Keep a narrowly scoped provider alias instead of broadening IAM.
provider "aws" {
  alias  = "untagged"
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
