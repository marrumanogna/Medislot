"""Milestone 7 guard rails: the infrastructure templates must keep meeting the hardening checklist."""
import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")
ROOT = pathlib.Path(__file__).resolve().parents[1]


class Loader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


Loader.add_multi_constructor("!", _tag)


def template(name):
    return yaml.load((ROOT / "cloudformation" / name).read_text(encoding="utf-8-sig"), Loader)["Resources"]


def of_type(resources, kind):
    return {k: v for k, v in resources.items() if v["Type"] == kind}


def test_dashboard_and_six_alarm_categories_exist():
    res = template("observability-stack.yaml")
    assert any("operations" in v["Properties"]["DashboardName"] for v in of_type(res, "AWS::CloudWatch::Dashboard").values())
    names = [v["Properties"]["AlarmName"] for v in of_type(res, "AWS::CloudWatch::Alarm").values()]
    for category in ("api-5xx", "api-latency", "-errors", "dlq", "reschedule", "rds-"):
        assert any(category in n for n in names), f"no alarm for category {category}"
    for alarm in of_type(res, "AWS::CloudWatch::Alarm").values():
        assert alarm["Properties"]["AlarmActions"] == ["AlarmTopic"], "every alarm must deliver to the SNS topic"


def test_budget_is_filtered_by_cost_allocation_tag():
    budget = next(iter(of_type(template("observability-stack.yaml"), "AWS::Budgets::Budget").values()))
    assert budget["Properties"]["Budget"]["CostFilters"]["TagKeyValue"] == ["user:Project$MediSlot"]


def test_everything_is_traced_with_xray():
    res = template("auth-stack.yaml")
    for name, fn in of_type(res, "AWS::Lambda::Function").items():
        assert fn["Properties"].get("TracingConfig") == {"Mode": "Active"}, f"{name} is not traced"
    machine = next(iter(of_type(res, "AWS::StepFunctions::StateMachine").values()))
    assert machine["Properties"]["TracingConfiguration"] == {"Enabled": True}
    assert next(iter(of_type(res, "AWS::ApiGateway::Stage").values()))["Properties"]["TracingEnabled"] is True


def _actions(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Action":
                yield from ([value] if isinstance(value, str) else value)
            else:
                yield from _actions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _actions(item)


@pytest.mark.parametrize("name", ["auth-stack.yaml", "iam-stack.yaml", "data-stack.yaml", "network-stack.yaml",
                                  "dashboard-stack.yaml", "observability-stack.yaml"])
def test_no_wildcard_iam_actions(name):
    bad = [a for a in _actions(template(name)) if isinstance(a, str) and (a == "*" or a.endswith(":*"))
           and not a.startswith(("sts:", "apigateway:"))]
    assert not bad, f"wildcard IAM actions in {name}: {bad}"


def test_kms_decrypt_is_scoped_to_ssm():
    text = (ROOT / "cloudformation" / "auth-stack.yaml").read_text(encoding="utf-8-sig")
    assert "kms:ViaService" in text and not re.search(r"kms:Decrypt\]?[^\n]*\n\s*Resource: '\*'", text)


def test_every_lambda_writes_logs():
    for path in (ROOT / "lambdas").glob("*/lambda_function.py"):
        source = path.read_text(encoding="utf-8-sig")
        assert re.search(r"\b(logger|log)\.(info|warning|exception|error)\(", source) or "logged(handler)" in source, path.parent.name


def test_backups_enabled_on_rds_and_every_dynamodb_table():
    res = template("data-stack.yaml")
    assert next(iter(of_type(res, "AWS::RDS::DBInstance").values()))["Properties"]["BackupRetentionPeriod"] >= 1
    for name, table in of_type(res, "AWS::DynamoDB::Table").items():
        assert table["Properties"]["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True, name


def test_pitr_declared_exactly_once_per_table():
    text = (ROOT / "cloudformation" / "data-stack.yaml").read_text(encoding="utf-8-sig")
    assert text.count("PointInTimeRecoveryEnabled") == text.count("AWS::DynamoDB::Table")
