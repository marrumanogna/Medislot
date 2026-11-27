# Runbook

## 1. One-time GitHub setup
| Name | Type | Used for |
|---|---|---|
| `AWS_DEPLOY_ROLE_ARN` | variable or secret | OIDC role the pipeline assumes |
| `NOTIFY_PATIENT_EMAIL`, `NOTIFY_DOCTOR_EMAIL` | secrets | Addresses subscribed to the patient and doctor SNS topics |
| `ALARM_EMAIL` | secret | Receives CloudWatch alarm and budget e-mails |
| `MONTHLY_BUDGET_USD` | variable (optional, default 25) | AWS Budget limit |

After the first deploy, open each inbox and click **Confirm subscription** (SNS sends nothing until you do).
In the AWS Billing console, activate the cost allocation tags **Project** and **Environment**.

## 2. Deploy
Push to a branch listed in `.github/workflows/deploy.yaml` (or run the workflow by hand). Order: IAM → network → SSM secrets → data → Lambda packaging → auth/API → observability → dashboard → migrations → tests → drift check.

## 3. Find the URLs
- API: `aws cloudformation describe-stacks --stack-name medislot-dev-auth --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text`
- Dashboard: EC2 console → instance `medislot-dev-dashboard` → public IPv4 (it changes if the instance is recreated).

## 4. When an alarm fires
| Alarm | First look |
|---|---|
| `api-5xx`, `*-errors` | CloudWatch Logs `/aws/lambda/medislot-dev-<function>`: search `FAILED`; every request logs `START` and `END` with the request id |
| `*-dlq-depth` | Open the DLQ in SQS, read a message, fix the cause, then redrive |
| `reschedule-execution-failed`, `reschedule-compensated` | Step Functions → `medislot-dev-reschedule` → open the execution and read the history; X-Ray service map shows where it broke |
| `rds-*` | RDS console: CPU, connections, storage |

## 5. Teardown (always in this order, one at a time)
observability → dashboard → auth → data → network → iam. Wait for each to finish.
Before deleting network, check that no network interface is left:
`aws ec2 describe-network-interfaces --filters "Name=vpc-id,Values=<vpc-id>" --query "NetworkInterfaces[].[NetworkInterfaceId,Status,Description]" --output table`
The two S3 buckets are `Retain` and RDS keeps a final snapshot: empty and delete those by hand for a fully clean account. Never create resources by hand inside the VPC; they block the subnet delete.

## 6. Problems we hit before
| Symptom | Cause and fix |
|---|---|
| Push rejected (non-fast-forward) | Local history diverged. `git pull --rebase`, never `--force` |
| Copying a zip over the project broke the app | Copy without `.git`: `robocopy <src> <dest> /E /XD .git`, and never overwrite newer files |
| Portal shows endless loading bars | A JavaScript file failed to load. Open DevTools → Console |
| Subnet `DELETE_FAILED` | A hand-made EC2 instance or interface still in the subnet. Remove it, retry |
| Actions run does not appear | Branch not in the workflow trigger list, or Actions disabled |
| SSH to EC2 fails | Port 22 is open only for `SshCidr`; prefer Session Manager |
