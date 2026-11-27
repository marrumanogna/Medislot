"""Report CloudFormation drift for every MediSlot stack.

Usage:  python scripts/check_drift.py medislot-dev-
Exit code 0 when every stack is IN_SYNC, 1 otherwise (drifted, failed or timed out).
Runs as the last (non-blocking) step of the deploy workflow and can be run by hand.
"""
import os
import sys
import time

import boto3

ACTIVE = ["CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "IMPORT_COMPLETE"]
TIMEOUT_SECONDS = 900


def stack_names(cf, prefix):
    pages = cf.get_paginator("list_stacks").paginate(StackStatusFilter=ACTIVE)
    return sorted(s["StackName"] for page in pages for s in page["StackSummaries"] if s["StackName"].startswith(prefix))


def detect(cf, names):
    """Start drift detection for every stack, then poll until all finish or the timeout passes."""
    pending = {n: cf.detect_stack_drift(StackName=n)["StackDriftDetectionId"] for n in names}
    results, deadline = {}, time.time() + TIMEOUT_SECONDS
    while pending and time.time() < deadline:
        for name, detection_id in list(pending.items()):
            status = cf.describe_stack_drift_detection_status(StackDriftDetectionId=detection_id)
            if status["DetectionStatus"] == "DETECTION_IN_PROGRESS":
                continue
            results[name] = (status.get("StackDriftStatus", "UNKNOWN") if status["DetectionStatus"] == "DETECTION_COMPLETE"
                             else "FAILED: " + status.get("DetectionStatusReason", "no reason given"))
            del pending[name]
        if pending:
            time.sleep(5)
    for name in pending:
        results[name] = "TIMED_OUT"
    return results


def drifted_resources(cf, name):
    drifts = cf.describe_stack_resource_drifts(
        StackName=name, StackResourceDriftStatusFilters=["MODIFIED", "DELETED"])["StackResourceDrifts"]
    return [f"{d['LogicalResourceId']} ({d['StackResourceDriftStatus']})" for d in drifts]


def main(prefix):
    cf = boto3.client("cloudformation", region_name=os.environ.get("AWS_REGION"))
    names = stack_names(cf, prefix)
    if not names:
        print(f"No stacks found with prefix '{prefix}'")
        return 1
    results = detect(cf, names)
    lines = ["| Stack | Drift status |", "|---|---|"] + [f"| {n} | {results[n]} |" for n in names]
    for name in names:
        if results[name] == "DRIFTED":
            lines.append(f"\nDrifted resources in {name}: " + ", ".join(drifted_resources(cf, name)))
    report = "\n".join(lines)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("### CloudFormation drift\n" + report + "\n")
    return 0 if all(v == "IN_SYNC" for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "medislot-"))
