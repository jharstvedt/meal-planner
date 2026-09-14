"""Transactional email notifications for household membership changes."""

import html
import logging
import os

import httpx

logger = logging.getLogger(__name__)

RESEND_EMAILS_URL = "https://api.resend.com/emails"
EMAIL_TIMEOUT_SECONDS = 5.0


async def send_household_member_notification(
    recipient_email: str, inviter_name: str | None, inviter_email: str, household_name: str
) -> bool:
    """Notify a user that they were added to a household.

    Returns False when email is not configured or Resend does not accept the
    message. Notification failures are contained so membership creation can
    remain successful.
    """
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    sender = os.getenv("EMAIL_FROM", "").strip()
    app_url = os.getenv("APP_URL", "").strip()
    missing = [
        name for name, value in (("RESEND_API_KEY", api_key), ("EMAIL_FROM", sender), ("APP_URL", app_url)) if not value
    ]
    if missing:
        logger.error("Email notification configuration missing: %s", ", ".join(missing))
        return False

    normalized_email = recipient_email.lower()
    inviter = f"{inviter_name} ({inviter_email})" if inviter_name else inviter_email
    subject = f"You were added to {household_name}"
    text = (
        f"{inviter} added you to the {household_name} household in Meal Planner.\n\n"
        f"Sign in with: {normalized_email}\n"
        f"Open Meal Planner: {app_url}"
    )
    html_body = (
        f"<p>{html.escape(inviter)} added you to the "
        f"<strong>{html.escape(household_name)}</strong> household in Meal Planner.</p>"
        f"<p>Sign in with: <strong>{html.escape(normalized_email)}</strong></p>"
        f'<p><a href="{html.escape(app_url, quote=True)}">Open Meal Planner</a></p>'
    )

    try:
        async with httpx.AsyncClient(timeout=EMAIL_TIMEOUT_SECONDS) as client:
            response = await client.post(
                RESEND_EMAILS_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"from": sender, "to": [normalized_email], "subject": subject, "text": text, "html": html_body},
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as error:
        logger.warning("Resend rejected household notification with HTTP %d", error.response.status_code)
        return False
    except httpx.RequestError as error:
        logger.warning("Resend household notification request failed: %s", type(error).__name__)
        return False
    except Exception:
        logger.exception("Unexpected error sending household notification")
        return False

    return True
