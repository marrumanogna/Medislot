# Disaster-recovery drill

## What is protected
| Store | Protection | Recovery point |
|---|---|---|
| RDS MySQL `medislot-<env>-mysql` | Automated backups (1-day retention) with point-in-time restore; final snapshot on stack delete | Any second within retention, up to about 5 minutes ago |
| DynamoDB tables (all six) | Point-in-time recovery enabled | Any second in the last 35 days |
| Infrastructure and code | Everything is CloudFormation in git; the pipeline rebuilds it from scratch | Last commit |

## Procedure
1. Run `python scripts/dr_drill.py dev` with administrator credentials. It restores the RDS instance to a temporary `…-drill` instance, restores `MediSlot-dev-Slots` to a temporary table, times both, then deletes them.
2. Paste the printed row into the result table below.
3. Optional proof of data: before deleting, connect to the drill instance from the dashboard EC2 host with a MySQL client and compare `SELECT COUNT(*) FROM appointments` with the source.

## Recovery objectives
- **RPO:** about 5 minutes for RDS, about 1 second for DynamoDB (point-in-time).
- **RTO:** the measured restore time below, plus about 15 minutes for the pipeline to repoint the stack if the instance must be replaced.

## Result (fill in after running the drill, then commit)
| Date | RDS restore point | RDS restore time | DynamoDB table | DynamoDB restore time | Notes |
|---|---|---|---|---|---|
| _run the script and paste its row here_ | | | | | |
