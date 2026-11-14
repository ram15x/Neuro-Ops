output "engine_instance_id" {
  value = aws_instance.engine.id
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "tag_to_add_to_managed_instances" {
  value       = "${local.managed_tag_key}=${local.managed_tag_value}"
  description = "the engine can only act on instances and groups that carry this tag"
}
