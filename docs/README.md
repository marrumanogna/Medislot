# MediSlot documentation index

Start with **review-guide.md** if you are preparing for the review.

| Document | What it answers |
|---|---|
| [review-guide.md](review-guide.md) | 30-minute demo script, code map and likely questions with answers |
| [architecture.md](architecture.md) | How the system fits together (diagram and the four main flows) |
| [data-model.md](data-model.md) | RDS tables and DynamoDB tables (generated) |
| [api.md](api.md) | Every endpoint, its auth and the Lambda behind it (generated) |
| [runbook.md](runbook.md) | Deploy, secrets, alarms, teardown and fixes for problems we hit |
| [test-report.md](test-report.md) | What is tested and how to run it |
| [iam-audit.md](iam-audit.md) | Least-privilege audit, including every remaining `Resource: "*"` |
| [dr-drill.md](dr-drill.md) | Disaster-recovery procedure and the measured restore time |
| [cost-review.md](cost-review.md) | Monthly cost estimate, cost allocation tags and the budget |
| [concurrency-design-note.md](concurrency-design-note.md) | How double booking is prevented |
| milestone-4.md, milestone-5.md, milestone-6.md | Per-milestone design notes |
| Medislot-AWS-Architecture-Flow.png | Architecture picture from Milestone 1 |

Regenerate the two generated files with `python scripts/generate_docs.py`.
