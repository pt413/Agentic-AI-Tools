import smtplib
from email.mime.text import MIMEText

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

SENDER_EMAIL = "rmstech25@gmail.com"
APP_PASSWORD = "yszd uzqu mixh rgvm"


def send_risk_email(
    label: str,
    score: float,
    email_text: str,
    llm_reason: str = "",
    stage: str = ""
):
    """
    Sends a risk alert email.

    Args:
        label: Detected risk labels
        score: Confidence score (BGE similarity)
        email_text: Original email content
        llm_reason: Explanation from LLM (if used)
        stage: Decision stage (bge / bart / llm)
    """

    subject = f"⚠ Risk Alert: {label}"

    # Optional sections
    stage_section = f"\nDecision Stage: {stage}" if stage else ""
    reason_section = f"\nReason: {llm_reason}" if llm_reason else ""

    body = f"""
Risk Detected!

Label: {label}
Confidence: {score}{stage_section}{reason_section}

Email Content:
{email_text[:1500]}
"""

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = SENDER_EMAIL

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, APP_PASSWORD)
        server.sendmail(SENDER_EMAIL, SENDER_EMAIL, msg.as_string())
        server.quit()

        print("✅ Risk email sent")

    except Exception as e:
        print("❌ Email failed:", e)