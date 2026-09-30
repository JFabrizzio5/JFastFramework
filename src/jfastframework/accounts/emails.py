"""The three emails accounts sends, built in and overridable.

* ``accounts/verify_email`` -- the link that proves an address is yours;
* ``accounts/reset_password`` -- the link that sets a new password;
* ``accounts/already_registered`` -- sent instead of a second account when
  somebody signs up with an address that has one, so the sign-up form does not
  have to say "that email is taken" to whoever is typing.

Each has a plain built-in version. A project replaces one by putting
``<name>.html`` (and optionally ``<name>.txt``) in the mail plugin's
``templates_dir``; the template gets ``app_name``, ``link``, ``email``,
``display_name`` and ``expires_minutes``. ``already_registered`` links to the
password reset page when reset is on, and to the sign-in page when it is not.

The token travels only inside ``link``. Nothing here logs it.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

__all__ = ["AccountEmails"]


@dataclass(frozen=True)
class _Copy:
    subject: str
    lead: str
    action: str
    after: str


_COPY: dict[str, _Copy] = {
    "verify_email": _Copy(
        subject="Confirm your email for {app}",
        lead="Confirm that this address is yours to finish setting up your {app} account.",
        action="Confirm email",
        after="The link works once and expires in {minutes} minutes. If you did not create "
        "an account, ignore this email.",
    ),
    "reset_password": _Copy(
        subject="Reset your {app} password",
        lead="Somebody asked to reset the password of the {app} account for this address.",
        action="Choose a new password",
        after="The link works once and expires in {minutes} minutes. Choosing a new password "
        "signs you out everywhere. If it was not you, ignore this email: your password "
        "has not changed.",
    ),
    "already_registered": _Copy(
        subject="You already have a {app} account",
        lead="Somebody tried to create a {app} account with this address, which already has one.",
        action="Reset your password",
        after="If it was you, sign in, or follow the link to choose a new password; it "
        "works once and expires in {minutes} minutes. If it was not you, ignore this email.",
    ),
    # The same, for a service without password reset: there is no link to reset with.
    "already_registered_no_reset": _Copy(
        subject="You already have a {app} account",
        lead="Somebody tried to create a {app} account with this address, which already has one.",
        action="Sign in",
        after="If it was you, sign in instead. If it was not you, ignore this email.",
    ),
}


class AccountEmails:
    def __init__(
        self,
        mailer: Any,
        *,
        app_name: str,
        frontend_url: str,
        verify_path: str,
        reset_path: str,
        login_path: str = "/login",
    ) -> None:
        self.mailer = mailer
        self.app_name = app_name
        self.frontend_url = frontend_url.rstrip("/")
        self.paths = {
            "verify_email": verify_path,
            "reset_password": reset_path,
            "already_registered": reset_path,
            "already_registered_no_reset": login_path,
        }

    def link(self, kind: str, token: str | None) -> str:
        if token is None:
            return f"{self.frontend_url}{self.paths[kind]}"
        return f"{self.frontend_url}{self.paths[kind]}?token={quote(token, safe='')}"

    def compose(
        self,
        kind: str,
        *,
        to: str,
        token: str | None,
        minutes: int,
        display_name: str | None = None,
    ) -> Any:
        copy = _COPY[kind]
        link = self.link(kind, token)
        subject = copy.subject.format(app=self.app_name)
        template = f"accounts/{kind.removesuffix('_no_reset')}"
        if self.mailer.has_template(template):
            return self.mailer.message(
                to=to,
                subject=subject,
                template=template,
                context={
                    "app_name": self.app_name,
                    "link": link,
                    "email": to,
                    "display_name": display_name,
                    "expires_minutes": minutes,
                },
            )
        lead = copy.lead.format(app=self.app_name)
        after = copy.after.format(minutes=minutes)
        greeting = f"Hello {display_name}," if display_name else "Hello,"
        text = f"{greeting}\n\n{lead}\n\n{copy.action}: {link}\n\n{after}\n"
        e = html.escape
        body = (
            f"<p>{e(greeting)}</p><p>{e(lead)}</p>"
            f'<p><a href="{e(link, quote=True)}">{e(copy.action)}</a></p>'
            f"<p>{e(after)}</p>"
        )
        return self.mailer.message(to=to, subject=subject, text=text, html=body)
