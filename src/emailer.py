import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Union

def send_email(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    from_email: str,
    to_email: Union[str, List[str]],
    subject: str,
    html: str,
):
    recipients = [to_email] if isinstance(to_email, str) else list(to_email)

    # Send individually (better deliverability + does not leak the list)
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)

        for r in recipients:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = from_email
            msg["To"] = r
            msg.attach(MIMEText(html, "html", "utf-8"))
            server.sendmail(from_email, [r], msg.as_string())
