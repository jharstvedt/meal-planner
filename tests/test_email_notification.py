"""Tests for household membership email notifications."""

import json
import os
from unittest.mock import patch

import httpx
import pytest
import respx

from api.services.email_notification import RESEND_EMAILS_URL, send_household_member_notification

EMAIL_ENV = {
    "RESEND_API_KEY": "test-api-key",
    "EMAIL_FROM": "Meal Planner <notifications@example.com>",
    "APP_URL": "https://app.example.com/sign-in?next=/home&mode=invite",
}


class TestSendHouseholdMemberNotification:
    """Tests for sending a household membership notification through Resend."""

    @pytest.mark.asyncio
    async def test_sends_required_notification_content(self) -> None:
        with patch.dict(os.environ, EMAIL_ENV, clear=True), respx.mock(assert_all_called=True) as resend_mock:
            route = resend_mock.post(RESEND_EMAILS_URL).mock(return_value=httpx.Response(200, json={"id": "email-id"}))

            result = await send_household_member_notification(
                recipient_email="New.Member@Example.com",
                inviter_name="Owner <Admin>",
                inviter_email="owner@example.com",
                household_name="Family & Friends",
            )

        request = route.calls.last.request
        payload = json.loads(request.content)
        assert result is True
        assert request.headers["Authorization"] == "Bearer test-api-key"
        assert payload["from"] == EMAIL_ENV["EMAIL_FROM"]
        assert payload["to"] == ["new.member@example.com"]
        assert payload["subject"] == "You were added to Family & Friends"
        assert "Owner <Admin> (owner@example.com)" in payload["text"]
        assert "Sign in with: new.member@example.com" in payload["text"]
        assert EMAIL_ENV["APP_URL"] in payload["text"]
        assert "Owner &lt;Admin&gt; (owner@example.com)" in payload["html"]
        assert "Family &amp; Friends" in payload["html"]
        assert "mode=invite" in payload["html"]
        assert "&amp;mode=invite" in payload["html"]

    @pytest.mark.asyncio
    async def test_returns_false_when_configuration_is_missing(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("api.services.email_notification.httpx.AsyncClient") as mock_client,
        ):
            result = await send_household_member_notification(
                recipient_email="new@example.com",
                inviter_name=None,
                inviter_email="owner@example.com",
                household_name="Family",
            )

        assert result is False
        mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_false_when_resend_rejects_message(self) -> None:
        with patch.dict(os.environ, EMAIL_ENV, clear=True), respx.mock(assert_all_called=True) as resend_mock:
            resend_mock.post(RESEND_EMAILS_URL).mock(return_value=httpx.Response(422, json={"message": "invalid"}))

            result = await send_household_member_notification(
                recipient_email="new@example.com",
                inviter_name=None,
                inviter_email="owner@example.com",
                household_name="Family",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_request_times_out(self) -> None:
        with patch.dict(os.environ, EMAIL_ENV, clear=True), respx.mock(assert_all_called=True) as resend_mock:
            resend_mock.post(RESEND_EMAILS_URL).mock(side_effect=httpx.TimeoutException("timed out"))

            result = await send_household_member_notification(
                recipient_email="new@example.com",
                inviter_name=None,
                inviter_email="owner@example.com",
                household_name="Family",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_unexpected_client_error(self) -> None:
        with (
            patch.dict(os.environ, EMAIL_ENV, clear=True),
            patch("api.services.email_notification.httpx.AsyncClient", side_effect=RuntimeError("unexpected")),
        ):
            result = await send_household_member_notification(
                recipient_email="new@example.com",
                inviter_name=None,
                inviter_email="owner@example.com",
                household_name="Family",
            )

        assert result is False
