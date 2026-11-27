# MediSlot

Doctor appointment platform on AWS: API Gateway, Lambda, RDS MySQL, DynamoDB, Step Functions, EventBridge, SQS, SNS, S3 and EC2.

## Submission note (Milestone 7)
The system is deployed from this repository by the GitHub Actions pipeline (`.github/workflows/deploy.yaml`).

| | |
|---|---|
| Web app URL | `<fill in: EC2 public IPv4 of medislot-dev-dashboard, http://...>` |
| API base URL | `<fill in: ApiUrl output of stack medislot-dev-auth>` |
| Region / environment | us-east-1 / dev |

Get the API URL with: `aws cloudformation describe-stacks --stack-name medislot-dev-auth --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text`

## Where to look
| Folder | Contents |
|---|---|
| `lambdas/<name>/lambda_function.py` | One folder per Lambda (20 folders, 22 deployed functions). `lambdas/common.py` is shared and is copied into each package |
| `cloudformation/` | `iam` → `network` → `data` → `auth` (API, Lambdas, saga, queues) → `observability` (dashboard, alarms, budget) → `dashboard` (EC2) |
| `dashboard/` | Flask web app: `app.py`, `templates/`, `static/js/{core,doctor,patient}.js` |
| `scehma/schema.sql` | RDS schema |
| `scripts/` | `deploy_stack.py`, `check_drift.py`, `dr_drill.py`, `generate_docs.py` |
| `tests/` | Unit tests (`pytest -q --ignore=tests/integration`); `tests/integration/` runs against the deployed API |
| `docs/` | Everything else: start at [docs/README.md](docs/README.md) |

## Documentation
[Review guide](docs/review-guide.md) · [Architecture](docs/architecture.md) · [Data model](docs/data-model.md) · [API](docs/api.md) · [Runbook](docs/runbook.md) · [Test report](docs/test-report.md) · [IAM audit](docs/iam-audit.md) · [DR drill](docs/dr-drill.md) · [Cost review](docs/cost-review.md)
