"""Email as a plugin: queued by default, templated, and safe in development.

    [plugins]
    enabled = ["observability", "queue", "mail"]

    [plugin.mail]
    backend = "smtp"          # console | smtp | memory
    host = "smtp.example.com"
    port = 587
    from_email = "billing@example.com"
    templates_dir = "templates/mail"

Credentials come from the environment (``JFAST_MAIL_USERNAME``,
``JFAST_MAIL_PASSWORD``) as ``SecretStr``, never from ``jfast.toml``. That is
not a style preference: a password with a default value in a committed file is
a password in the repository forever, and it is the single most common way an
SMTP account gets taken over and used to send spam in your name.

Sending is **queued** when the ``queue`` plugin is enabled, which is the
default. ``mail.send()`` enqueues and returns in microseconds; the worker does
the SMTP conversation, and the queue's retry and dead-lettering apply to it.
``mail.send_now()`` is the synchronous escape hatch and reads like one at the
call site.

Requires: ``pip install jfastframework[mail]``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from jfastframework.mail.backends import ConsoleMailer, MailBackend, MemoryMailer, SMTPMailer
from jfastframework.mail.message import DEFAULT_MAX_ATTACHMENT_BYTES, Attachment, EmailMessage
from jfastframework.mail.templates import TemplateRenderer
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from jfastframework.context import AppContext

SEND_TASK = "jfast.mail.send"


class MailSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_MAIL_", env_file=".env", extra="ignore")

    # console outside production: nobody emails a real customer from a laptop.
    backend: str = "console"
    host: str = "localhost"
    port: int = 587
    username: str = ""
    password: SecretStr = SecretStr("")
    from_email: str = ""
    use_starttls: bool = True
    use_ssl: bool = False
    timeout: float = 30.0
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES

    templates_dir: str = "templates/mail"
    # Off makes every send synchronous. Useful in a script, wrong in a request.
    queued: bool = True


class Mailer:
    """What ``ctx.require("mail")`` gives you."""

    def __init__(
        self,
        backend: MailBackend,
        *,
        default_from: str,
        templates: TemplateRenderer | None = None,
        queue: Any = None,
    ) -> None:
        self._backend = backend
        self._default_from = default_from
        self._templates = templates
        self._queue = queue

    # -- composing -----------------------------------------------------

    def message(
        self,
        *,
        to: list[str] | str,
        subject: str,
        template: str | None = None,
        context: dict[str, Any] | None = None,
        text: str = "",
        html: str = "",
        **extra: Any,
    ) -> EmailMessage:
        """Build a message, rendering a template when one is named."""
        if template is not None:
            if self._templates is None:
                raise RuntimeError(
                    f"No template directory configured, so {template!r} cannot be "
                    f"rendered. Set [plugin.mail] templates_dir."
                )
            html, text = self._templates.render(template, context)
        return EmailMessage(
            to=[to] if isinstance(to, str) else list(to),
            subject=subject,
            text=text,
            html=html,
            from_email=extra.pop("from_email", "") or self._default_from,
            **extra,
        )

    def has_template(self, name: str) -> bool:
        """Whether the project's templates directory has ``<name>.html``.

        For code with a built-in message of its own -- the accounts emails --
        that lets a project override it by dropping a file in, without the
        built-in failing on a project that has no templates at all.
        """
        return self._templates is not None and self._templates.exists(name)

    # -- sending -------------------------------------------------------

    async def send(self, message: EmailMessage) -> str:
        """Queue the message. Returns the job id, or ``"sent"`` if unqueued.

        The default path, and the one to reach for inside a request handler: a
        mail server being slow or briefly refusing should not become the
        latency or the error of the request that triggered it.
        """
        if self._queue is None:
            await self._backend.send(message)
            return "sent"

        from jfastframework.queues.base import Job

        job = Job(task=SEND_TASK, payload={"message": message.to_json()})
        job_id: str = await self._queue.enqueue(job)
        return job_id

    async def send_now(self, message: EmailMessage) -> None:
        """Send synchronously, waiting for the server.

        Named so the call site admits what it is doing. Correct for a one-time
        password or a test; wrong for anything a user is waiting on.
        """
        await self._backend.send(message)

    @property
    def backend(self) -> MailBackend:
        return self._backend


#: Backends that accept a message and deliver nothing, and what each does with
#: it instead. Both are right for development and neither can be right in
#: production, where the only symptom is a customer who never got the email.
SILENT_BACKENDS = {
    "console": "messages are printed to stdout",
    "memory": "messages are kept in a list nobody reads",
}


def build_backend(settings: MailSettings) -> MailBackend:
    if settings.backend == "console":
        return ConsoleMailer(default_from=settings.from_email or "noreply@localhost")
    if settings.backend == "memory":
        return MemoryMailer()
    if settings.backend == "smtp":
        return SMTPMailer(
            host=settings.host,
            port=settings.port,
            username=settings.username,
            password=settings.password.get_secret_value(),
            default_from=settings.from_email,
            use_starttls=settings.use_starttls,
            use_ssl=settings.use_ssl,
            timeout=settings.timeout,
            max_attachment_bytes=settings.max_attachment_bytes,
        )
    raise ValueError(f"Unknown mail backend {settings.backend!r}. Use console, smtp or memory.")


class MailPlugin(Plugin):
    meta = PluginMeta(
        name="mail",
        version="0.1.0",
        description="Email with templates, queued by default.",
        after=("observability", "queue"),
        provides=("mail",),
        default_enabled=False,
        extra="jfastframework[mail]",
        # A mail server being down degrades the service; it does not break it.
        health_critical=False,
    )
    Settings = MailSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._mailer: Mailer | None = None

    def register(self, ctx: AppContext) -> None:
        settings: MailSettings = self.settings

        missing_credentials = not settings.username or not settings.password.get_secret_value()
        if settings.backend == "smtp" and ctx.settings.is_production and missing_credentials:
            raise ValueError(
                "The smtp backend needs JFAST_MAIL_USERNAME and "
                "JFAST_MAIL_PASSWORD. Refusing to start in production with "
                "credentials missing, rather than failing on the first send."
            )

        # The default backend is `console`, deliberately: nobody emails a real
        # customer from a laptop. In production it means every message is
        # written to stdout and none is sent -- a verification link that never
        # arrives, a password reset that never arrives, an invoice that never
        # arrives, and a `send` that returned successfully for all three. There
        # is no error to find, no bounce, and no queue backing up; the only
        # symptom is customers saying they got nothing.
        #
        # Refused rather than warned for the same reason as the smtp branch
        # above: this is not a degraded mode, it is silence.
        if settings.backend in SILENT_BACKENDS and ctx.settings.is_production:
            raise ValueError(
                f"the mail backend is {settings.backend!r} in production, which sends "
                f"nothing: {SILENT_BACKENDS[settings.backend]}, and `send` reports "
                f'success either way. Set [plugin.mail] backend = "smtp" with '
                f"JFAST_MAIL_USERNAME and JFAST_MAIL_PASSWORD, or disable the mail "
                f"plugin if this service sends no mail."
            )

        backend = build_backend(settings)

        templates: TemplateRenderer | None = None
        from pathlib import Path

        if Path(settings.templates_dir).is_dir():
            templates = TemplateRenderer(settings.templates_dir)

        queue = ctx.optional("queue") if settings.queued else None
        mailer = Mailer(
            backend,
            default_from=settings.from_email or settings.username or "noreply@localhost",
            templates=templates,
            queue=queue,
        )
        self._mailer = mailer
        ctx.provide("mail", mailer)

        # Register the worker task, so `jfast worker` drains the mail queue
        # without the application having to wire anything.
        tasks = ctx.optional("tasks")
        if tasks is not None:

            async def handle(payload: dict[str, Any]) -> None:
                await backend.send(EmailMessage.from_json(payload["message"]))

            tasks.register(SEND_TASK, handle)

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._mailer is None:
            return HealthReport.fail("mailer not initialised", critical=False)
        healthy, detail = await self._mailer.backend.health()
        settings: MailSettings = self.settings
        return (
            HealthReport.ok(detail, backend=settings.backend, queued=settings.queued)
            if healthy
            else HealthReport.fail(detail, critical=False)
        )

    def describe(self) -> dict[str, Any]:
        settings: MailSettings = self.settings
        described = super().describe()
        described["backend"] = settings.backend
        described["queued"] = settings.queued
        return described


__all__ = ["SEND_TASK", "Attachment", "EmailMessage", "MailPlugin", "MailSettings", "Mailer"]
