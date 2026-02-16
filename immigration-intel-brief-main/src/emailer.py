import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

def send_email(smtp_host, smtp_port, smtp_user, smtp_pass, from_email, to_email, subject, html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email

    msg.attach(MIMEText("This email requires HTML viewing.", "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(from_email, [to_email], msg.as_string())
