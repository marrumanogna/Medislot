# IAM audit

**Rule:** no wildcard actions (`*` or `service:*`) and every role scoped to specific resource ARNs. This is enforced in CI by `tests/test_m7_templates.py::test_no_wildcard_iam_actions`.

## Wildcard actions
None in any template.

## Lambda roles (`auth-stack.yaml`)
- One role per function, each limited to the SSM parameters, tables, queues, topics and functions it uses, all named `medislot-<env>-*`.
- `kms:Decrypt` (to read SecureString parameters) is limited to keys in this account and region, **and** only when the call comes through SSM (`kms:ViaService`). It is not `Resource: "*"`.

## Remaining `Resource: "*"` (each is required by AWS and cannot be narrowed)
| Where | Actions | Why a wildcard is unavoidable |
|---|---|---|
| Lambda base policy | `ec2:CreateNetworkInterface`, `Describe…`, `Delete…`, `Assign/UnassignPrivateIpAddresses` | Lambdas in a VPC create network interfaces; these actions do not support resource ARNs |
| Lambda base policy, state machine role | `xray:PutTraceSegments`, `PutTelemetryRecords` (and sampling reads) | X-Ray write actions have no resource-level permissions |
| Deploy role: `ReadAndValidateMediSlotCloudFormation` | `DescribeStacks`, `ListStacks`, `ValidateTemplate` | List and validate calls are not resource-scoped |
| Deploy role: `ManageMediSlotNetworkResources`, `DescribeResourcesForDashboardUpdate` | EC2 create and describe actions | EC2 create calls cannot name the resource before it exists |
| Deploy role: `CreateMediSlotRDSInstances`, `DescribeMediSlotRDSInstances`, `ManageMediSlotRDSSnapshots` | RDS create and describe | Same reason; modify and delete are scoped to `medislot-*` |
| Deploy role: `ManageLambdaEventSourceMappings` | Event source mapping actions | Mappings have no ARN until created |
| Deploy role: `DescribeAlarms`, `ReadDriftStatus` | `cloudwatch:DescribeAlarms`, `cloudformation:DescribeStackDriftDetectionStatus` | Read-only calls with no resource-level support |

Everything that creates or changes data (alarms, dashboards, budgets, buckets, tables, queues, topics, functions, roles) is limited to names starting `medislot-<env>-` or `MediSlot-<env>-`.

## Tags
Each stack is deployed with `Project`, `Environment` and `ManagedBy` tags (see `scripts/deploy_stack.py`), which the deploy role may apply to every service it creates.

## How to re-audit
`pytest tests/test_m7_templates.py`, then search templates for `Resource: '*'` and compare with the table above.
