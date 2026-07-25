"""Отправка отчётов по SMTP; список получателей передаётся вызывающим кодом."""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from typing import List, Union

from ..config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD

logger = logging.getLogger(__name__)


def send_report_email(
    subject: str,
    body: str,
    attachments: List[Union[Path, tuple]],
    recipients: List[str],
) -> bool:
    """Вложения: Path или (filename, bytes)."""
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        logger.error("SMTP не настроен: задайте SMTP_HOST, SMTP_USER, SMTP_PASSWORD")
        return False

    if not recipients:
        logger.error("Нет получателей письма")
        return False
    to_list = recipients

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(to_list)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    for item in attachments:
        if isinstance(item, (Path, str)):
            path = Path(item)
            if not path.exists():
                logger.warning("Вложение не найдено: %s", path)
                continue
            filename = path.name
            data = path.read_bytes()
        else:
            filename, data = item

        part = MIMEBase("application", "octet-stream")
        part.set_payload(data)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", filename))
        msg.attach(part)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.sendmail(SMTP_USER, to_list, msg.as_string())
        logger.info("Письмо отправлено на %s", to_list)
        return True
    except Exception as e:
        logger.exception("Ошибка отправки письма: %s", e)
        return False
