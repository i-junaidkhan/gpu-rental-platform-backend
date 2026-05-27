import os
import requests
import logging

logger = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

def send_slack_notification(message: str):
    if not SLACK_WEBHOOK_URL:
        logger.warning("SLACK_WEBHOOK_URL not set, cannot send Slack notification")
        return False
    payload = {"text": message}
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=5)
        resp.raise_for_status()
        logger.info(f"Slack notification sent: {message[:100]}")
        return True
    except Exception as e:
        logger.error(f"Failed to send Slack notification: {e}")
        return False