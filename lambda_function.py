import json
import boto3
import os
import uuid
from datetime import datetime, timedelta, timezone

# ── Settings ──────────────────────────────────────────────────────────────────
REGION        = os.environ.get("REGION", "ap-south-2")
SLACK_URL     = os.environ.get("SLACK_WEBHOOK_URL", "")
DB_TABLE      = "AIOpsIncidents"
ASG_NAME      = os.environ.get("ASG_NAME", "AIOps-ASG")

# ── AWS Clients ───────────────────────────────────────────────────────────────
bedrock     = boto3.client("bedrock-runtime", region_name=REGION)
cloudwatch  = boto3.client("cloudwatch", region_name=REGION)
ssm         = boto3.client("ssm", region_name=REGION)
dynamodb    = boto3.resource("dynamodb", region_name=REGION)
autoscaling = boto3.client("autoscaling", region_name=REGION)

# ── Main Lambda Handler ───────────────────────────────────────────────────────
def lambda_handler(event, context):

    print("Event received:", json.dumps(event))

    # 1. Extract alarm details
    alarm_name = event.get("detail", {}).get("alarmName", "Unknown Alarm")
    reason     = event.get("detail", {}).get("state", {}).get("reason", "")

    print(f"Alarm: {alarm_name}")

    # 2. Get CPU metrics
    cpu_values = get_recent_cpu()

    print(f"CPU values (last 10 min): {cpu_values}")

    latest_cpu = cpu_values[-1] if cpu_values else 0
    max_cpu    = max(cpu_values) if cpu_values else 0

    # 3. Smarter spike detection
    is_spike = latest_cpu > 60 or max_cpu > 80

    if not is_spike:
        print("No significant anomaly detected.")

        return {
            "status": "skipped"
        }

    # 4. Ask Claude for RCA + action
    claude_result = ask_claude(
        alarm_name,
        reason,
        cpu_values,
        latest_cpu,
        max_cpu
    )

    print("Claude Result:", claude_result)

    # 5. Execute remediation
    action_result = take_action(claude_result)

    print("Action Result:", action_result)

    # 6. Save incident
    incident_id = save_to_db(
        alarm_name,
        latest_cpu,
        max_cpu,
        claude_result,
        action_result
    )

    # 7. Send Slack alert
    send_slack(
        alarm_name,
        latest_cpu,
        max_cpu,
        incident_id,
        claude_result,
        action_result
    )

    return {
        "status": "success",
        "incident_id": incident_id
    }


# ── Get CloudWatch CPU Metrics ────────────────────────────────────────────────
def get_recent_cpu():

    now   = datetime.now(timezone.utc)
    start = now - timedelta(minutes=10)

    try:
        resp = cloudwatch.get_metric_statistics(
            Namespace  = "AWS/EC2",
            MetricName = "CPUUtilization",
            Dimensions = [{"Name": "AutoScalingGroupName", "Value": ASG_NAME}],

            StartTime  = start,
            EndTime    = now,
            Period     = 60,
            Statistics = ["Average"]
        )

        points = sorted(
            resp["Datapoints"],
            key=lambda x: x["Timestamp"]
        )

        return [round(p["Average"], 1) for p in points]

    except Exception as e:

        print(f"CloudWatch error: {e}")

        return [10.0, 15.0, 20.0, 85.0, 91.0]


# ── AI RCA + Intelligent Decision Engine ──────────────────────────────────────
def ask_claude(
    alarm_name,
    reason,
    cpu_values,
    latest_cpu,
    max_cpu
):

    prompt = f"""
You are an expert AWS SRE and AIOps engineer.

Analyze the following EC2 anomaly.

ALARM NAME:
{alarm_name}

CLOUDWATCH REASON:
{reason}

CPU READINGS (last 10 minutes):
{cpu_values}

LATEST CPU:
{latest_cpu}%

MAX CPU:
{max_cpu}%

Choose ONE action:

- SCALE
  For sustained CPU spikes above 90%, choose SCALE unless there is strong evidence of application failure.

- RESTART
  If CPU spike appears caused by runaway processes, unhealthy services, stuck applications, or abnormal behavior, choose RESTART.


- ALERT_ONLY
  If anomaly appears temporary or evidence is insufficient, choose ALERT_ONLY.

- NO_ACTION
  If CPU quickly returns to normal levels, choose NO_ACTION

Return ONLY valid JSON.

Example:

{{
  "cause": "Traffic spike detected",
  "severity": "HIGH",
  "action": "SCALE",
  "explanation": "Sustained high CPU likely caused by increased traffic load"
}}
"""

    try:

        response = bedrock.invoke_model(

            modelId="global.anthropic.claude-sonnet-4-5-20250929-v1:0",

            contentType="application/json",

            accept="application/json",

            body=json.dumps({

                "anthropic_version": "bedrock-2023-05-31",

                "max_tokens": 400,

                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            })
        )

        response_body = json.loads(
            response["body"].read()
        )

        text = response_body["content"][0]["text"]

        text = text.replace("```json", "").replace("```", "").strip()

        return json.loads(text)

    except Exception as e:

        print(f"Bedrock error: {e}")

        return {

            "cause": "Unable to determine exact cause",

            "severity": "HIGH",

            "action": "ALERT_ONLY",

            "explanation": "Fallback action triggered because AI analysis failed"
        }

def get_asg_instances():

    try:

        response = autoscaling.describe_auto_scaling_groups(
            AutoScalingGroupNames=[ASG_NAME]
        )

        groups = response.get("AutoScalingGroups", [])

        if not groups:
            return []

        instances = groups[0].get("Instances", [])

        instance_ids = [
            i["InstanceId"]
            for i in instances
            if i["LifecycleState"] == "InService"
        ]

        print("ASG Instances:", instance_ids)

        return instance_ids

    except Exception as e:

        print(f"ASG instance fetch error: {e}")

        return []

# ── Intelligent Remediation Engine ────────────────────────────────────────────
def take_action(claude_result):

    action = claude_result.get("action", "NO_ACTION")

    # ── SCALE ──────────────────────────────────────────────────────────────
    if action == "SCALE":

        try:

            autoscaling.set_desired_capacity(

                AutoScalingGroupName=ASG_NAME,

                DesiredCapacity=2,

                HonorCooldown=False
            )

            return {

                "action": "SCALE",

                "status": "success",

                "detail": "Auto Scaling Group scaled to 2 instances"
            }

        except Exception as e:

            return {

                "action": "SCALE",

                "status": "failed",

                "detail": str(e)
            }

    # ── RESTART ────────────────────────────────────────────────────────────
    elif action == "RESTART":

        try:

            resp = ssm.send_command(

                InstanceIds=get_asg_instances(),

                DocumentName="AWS-RunShellScript",

                Parameters={

                    "commands": [

                        "echo 'AIOps restarting service'",

                        "sudo systemctl restart httpd 2>/dev/null || echo 'httpd not found'",

                        "echo 'Restart complete'"
                    ]
                },

                Comment="AIOps intelligent remediation"
            )

            cmd_id = resp["Command"]["CommandId"]

            return {

                "action": "RESTART",

                "status": "success",

                "detail": f"Restart command sent. ID: {cmd_id}"
            }

        except Exception as e:

            return {

                "action": "RESTART",

                "status": "failed",

                "detail": str(e)
            }

    # ── ALERT ONLY ─────────────────────────────────────────────────────────
    elif action == "ALERT_ONLY":

        return {

            "action": "ALERT_ONLY",

            "status": "success",

            "detail": "Alert sent for human investigation"
        }

    # ── NO ACTION ──────────────────────────────────────────────────────────
    else:

        return {

            "action": "NO_ACTION",

            "status": "skipped",

            "detail": "No remediation required"
        }


# ── Save Incident to DynamoDB ─────────────────────────────────────────────────
def save_to_db(
    alarm_name,
    latest_cpu,
    max_cpu,
    claude_result,
    action_result
):

    incident_id = str(uuid.uuid4())[:8].upper()

    timestamp = datetime.now(timezone.utc).isoformat()

    try:

        table = dynamodb.Table(DB_TABLE)

        table.put_item(

            Item={

                "incident_id": incident_id,

                "timestamp": timestamp,

                "alarm_name": alarm_name,

                "latest_cpu": str(latest_cpu),

                "max_cpu": str(max_cpu),

                "cause": claude_result.get("cause", ""),

                "severity": claude_result.get("severity", ""),

                "ai_action": claude_result.get("action", ""),

                "action_status": action_result.get("status", ""),

                "detail": action_result.get("detail", ""),

                "explanation": claude_result.get("explanation", "")
            }
        )

        print(f"Saved incident: {incident_id}")

    except Exception as e:

        print(f"DynamoDB error: {e}")

    return incident_id


# ── Slack Notifications ───────────────────────────────────────────────────────
def send_slack(
    alarm_name,
    latest_cpu,
    max_cpu,
    incident_id,
    claude_result,
    action_result
):

    import urllib.request

    if not SLACK_URL:

        print("Slack webhook missing.")

        return

    severity = claude_result.get("severity", "HIGH")

    emoji = {

        "LOW": "🟡",

        "MEDIUM": "🟠",

        "HIGH": "🔴",

        "CRITICAL": "🚨"

    }.get(severity, "⚠️")

    message = {

        "text": (

            f"{emoji} *AIOps Incident Detected*\n\n"

            f"*Alarm:* {alarm_name}\n"

            f"*Latest CPU:* {latest_cpu}%\n"

            f"*Max CPU:* {max_cpu}%\n"

            f"*Severity:* {severity}\n"

            f"*Incident ID:* `{incident_id}`\n\n"

            f"*AI Root Cause Analysis:*\n"
            f"{claude_result.get('cause', 'Unknown')}\n\n"

            f"*AI Decision:* "
            f"{claude_result.get('action', 'UNKNOWN')}\n\n"

            f"*Explanation:*\n"
            f"{claude_result.get('explanation', '')}\n\n"

            f"*Remediation Status:*\n"
            f"{action_result.get('detail', '')}"
        )
    }

    try:

        data = json.dumps(message).encode("utf-8")

        req = urllib.request.Request(

            SLACK_URL,

            data=data,

            headers={
                "Content-Type": "application/json"
            }
        )

        urllib.request.urlopen(req, timeout=10)

        print("Slack message sent!")

    except Exception as e:

        print(f"Slack error: {e}")