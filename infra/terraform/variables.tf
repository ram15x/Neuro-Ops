variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "name" {
  type    = string
  default = "neuroops"
}

variable "alert_email" {
  type        = string
  description = "where SNS alerts go. you have to confirm the subscription from your inbox"
}

variable "engine_mode" {
  type        = string
  default     = "shadow"
  description = "shadow records decisions only. start here and read the audit log before moving to approval or auto"
  validation {
    condition     = contains(["shadow", "approval", "auto"], var.engine_mode)
    error_message = "engine_mode must be shadow, approval or auto."
  }
}

variable "instance_type" {
  type    = string
  default = "t3.micro"
}

variable "api_allowed_cidr" {
  type        = string
  description = "who can reach the API on port 8000, for example your own IP as 203.0.113.7/32. no default on purpose"
  validation {
    condition     = can(cidrhost(var.api_allowed_cidr, 0)) && var.api_allowed_cidr != "0.0.0.0/0"
    error_message = "Give a real CIDR block, and not 0.0.0.0/0."
  }
}

variable "image_uri" {
  type        = string
  description = "container image to run, for example ghcr.io/ram15x/neuroops:latest"
}
