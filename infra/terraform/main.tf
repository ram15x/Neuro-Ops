# Runs the engine on one small instance in the default VPC and gives it only the
# permissions the executors need. Nothing here has been applied to a real account:
# CI runs `terraform validate` and that is all.

data "aws_vpc" "default" {
  default = true
}

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }
}

locals {
  # instances the engine is allowed to act on carry this tag, nothing else is touched
  managed_tag_key   = "neuroops:managed"
  managed_tag_value = "true"
}

resource "aws_security_group" "api" {
  name        = "${var.name}-api"
  description = "engine API, one allowed address, no SSH (use Session Manager)"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "API"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.api_allowed_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---- permissions ----
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "engine" {
  name               = "${var.name}-engine"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

# Session Manager access to the engine box itself, and the CloudWatch agent
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.engine.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "cw_agent" {
  role       = aws_iam_role.engine.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

data "aws_iam_policy_document" "engine" {
  # reading is allowed everywhere, these calls cannot change anything
  statement {
    sid       = "ReadOnly"
    actions   = ["cloudwatch:GetMetricData", "cloudwatch:ListMetrics", "ec2:DescribeInstances",
                 "ec2:DescribeInstanceCreditSpecifications", "autoscaling:DescribeAutoScalingGroups"]
    resources = ["*"]
  }

  # changes are only allowed on instances carrying the managed tag
  statement {
    sid       = "ActOnTaggedInstances"
    actions   = ["ec2:RebootInstances", "ec2:StopInstances", "ec2:StartInstances"]
    resources = ["arn:aws:ec2:${var.region}:${data.aws_caller_identity.me.account_id}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/${local.managed_tag_key}"
      values   = [local.managed_tag_value]
    }
  }

  # this call does not support resource-level permissions, so it cannot be scoped by tag.
  # the engine only calls it for the enable_unlimited action, which always needs approval.
  statement {
    sid       = "CreditMode"
    actions   = ["ec2:ModifyInstanceCreditSpecification"]
    resources = ["*"]
  }

  # shell commands go through the managed document only, to tagged instances only
  statement {
    sid       = "RunCommandOnTaggedInstances"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ec2:${var.region}:${data.aws_caller_identity.me.account_id}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/${local.managed_tag_key}"
      values   = [local.managed_tag_value]
    }
  }
  statement {
    sid       = "RunCommandDocument"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ssm:${var.region}::document/AWS-RunShellScript"]
  }
  statement {
    sid       = "RunCommandResult"
    actions   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
    resources = ["*"]
  }

  statement {
    sid       = "ScaleTaggedGroups"
    actions   = ["autoscaling:SetDesiredCapacity"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/${local.managed_tag_key}"
      values   = [local.managed_tag_value]
    }
  }

  # the two secrets you create by hand, see README
  statement {
    sid       = "ReadOwnSecrets"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.me.account_id}:parameter/${var.name}/*"]
  }

  statement {
    sid       = "Notify"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

data "aws_caller_identity" "me" {}

resource "aws_iam_role_policy" "engine" {
  name   = "${var.name}-engine"
  role   = aws_iam_role.engine.id
  policy = data.aws_iam_policy_document.engine.json
}

resource "aws_iam_instance_profile" "engine" {
  name = "${var.name}-engine"
  role = aws_iam_role.engine.name
}

# ---- memory and disk need the CloudWatch agent, EC2 does not report them by itself ----
resource "aws_ssm_parameter" "cw_agent" {
  name = "/${var.name}/cwagent-config"
  type = "String"
  value = jsonencode({
    metrics = {
      namespace = "CWAgent"
      append_dimensions = { InstanceId = "$${aws:InstanceId}" }
      metrics_collected = {
        mem  = { measurement = ["mem_used_percent"], metrics_collection_interval = 60 }
        disk = { measurement = ["used_percent"], resources = ["/"], metrics_collection_interval = 60 }
      }
    }
  })
}

# ---- the engine box ----
resource "aws_instance" "engine" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  iam_instance_profile   = aws_iam_instance_profile.engine.name
  vpc_security_group_ids = [aws_security_group.api.id]
  monitoring             = true

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  root_block_device {
    encrypted   = true
    volume_size = 12
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    image_uri = var.image_uri
    mode      = var.engine_mode
    region    = var.region
    cw_param  = aws_ssm_parameter.cw_agent.name
    name      = var.name
  })

  tags = { Name = "${var.name}-engine" }
}

# ---- a second opinion that does not depend on the engine being alive ----
resource "aws_cloudwatch_metric_alarm" "engine_down" {
  alarm_name          = "${var.name}-engine-status-check"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = { InstanceId = aws_instance.engine.id }
  statistic           = "Maximum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  period              = 60
  evaluation_periods  = 3
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
