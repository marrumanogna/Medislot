# Cost review

All figures are **estimates** for one dev environment in us-east-1 running 24/7 at demo load. Check real numbers in Cost Explorer filtered by the tag `Project = MediSlot` (activate the tag in Billing first; it takes about a day to appear).

| Item | Basis | ~USD / month |
|---|---|---|
| RDS MySQL `db.t3.micro`, single AZ | about $0.017/h | 12 |
| RDS storage 20 GB + 1-day backups | gp2, backups within free allowance | 3 |
| EC2 dashboard `t3.micro` + 8 GB disk | about $0.0104/h | 8 |
| Public IPv4 address | $0.005/h | 4 |
| Interface VPC endpoints (ssm, events, states) | $0.01/h each per AZ: 22 per AZ for the three | 22 to 44 |
| Lambda, API Gateway, DynamoDB on-demand, SQS, SNS, Step Functions, EventBridge | demo traffic, mostly free tier | 1 to 3 |
| CloudWatch: 12 alarms, 1 dashboard, logs | first 10 alarms and 3 dashboards free | 2 |
| X-Ray | 100 000 traces free | 0 |
| AWS Budgets | first 2 budgets free | 0 |
| **Total** | | **about 52 to 76** |

## Where the money goes and how to cut it
1. **Interface endpoints are the largest line.** They let VPC Lambdas reach SSM, EventBridge and Step Functions without a NAT gateway (a NAT would cost more, about $33 a month). Using one AZ halves it.
2. **Tear down after demos.** Delete stacks in the runbook order; RDS and EC2 are about 60 percent of the rest. Stop (not delete) the dashboard instance between sessions.
3. **Everything else is pay-per-use** and near zero at demo volume.

## Controls in place
- Stack tags `Project`, `Environment`, `ManagedBy` on every taggable resource (`scripts/deploy_stack.py`).
- `medislot-<env>-monthly` budget (default $25, variable `MONTHLY_BUDGET_USD`) filtered by the Project tag; alerts at 80 percent actual and 100 percent forecast go to the alarm SNS topic and `ALARM_EMAIL`.
- Raise `MONTHLY_BUDGET_USD` to match the estimate above if you keep everything running.
