# Terraform

One engine instance, a locked-down IAM role, the CloudWatch agent config for memory and disk, an SNS topic and a status alarm.

This has never been applied. CI runs `terraform fmt -check` and `terraform validate`, nothing more. Read the plan before you apply it.

What to know before using it:
- The engine can only change instances and Auto Scaling groups tagged `neuroops:managed=true`. Tag only what you want it to touch.
- `ec2:ModifyInstanceCreditSpecification` cannot be scoped by tag in IAM, so it is open. The engine only calls it for an action that always needs a person's approval.
- Start with `engine_mode = "shadow"` and read the audit log for a few days.
- Before applying, create two SecureString parameters by hand so the secrets stay out of terraform state:
  `aws ssm put-parameter --name /neuroops/jwt-secret --type SecureString --value "$(openssl rand -base64 48)"`
  `aws ssm put-parameter --name /neuroops/admin-password --type SecureString --value "<a long password>"`
  The app refuses to start in production with the default values.
- The instance runs the engine against the simulated fleet unless you point it at a real account. The CloudWatch source exists in the code but is not wired into the running loop yet.
- The API is plain HTTP on one allowed address. Put it behind an ALB with TLS before opening it up.
