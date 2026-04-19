"""Typed data models shared across stages.

A few principles:

* Every artifact the orchestrator writes is a Pydantic model so it
  round-trips through YAML/JSON without losing type information.
* Field names are kept short — these objects ride inside LLM prompts
  and shorter keys mean fewer tokens.
* Models hold *no* secrets. Login credentials only ever live in the
  process environment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class URLKind(str, Enum):
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    LOGIN = "login"
    REDIRECT_TO_LOGIN = "redirect_to_login"
    POST_LOGIN_LANDING = "post_login_landing"
    UNKNOWN = "unknown"


class Status(str, Enum):
    PENDING = "pending"
    EXTRACTED = "extracted"
    POM_READY = "pom_ready"
    FEATURE_READY = "feature_ready"
    STEPS_READY = "steps_ready"
    COMPLETE = "complete"
    NEEDS_IMPLEMENTATION = "needs_implementation"
    FAILED = "failed"


class SelectorStrategy(str, Enum):
    """In priority order — first that resolves wins."""

    TEST_ID = "test_id"
    ROLE_NAME = "role_name"
    LABEL = "label"
    PLACEHOLDER = "placeholder"
    TEXT = "text"
    CSS = "css"
    XPATH = "xpath"


class StableSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: SelectorStrategy
    value: str
    role: str | None = None
    name: str | None = None
    nth: int | None = None


class Element(BaseModel):
    """One automation-relevant element on a page."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable slug used as a method/key name")
    role: str
    name: str | None = None
    kind: Literal[
        "button",
        "link",
        "input",
        "textarea",
        "select",
        "checkbox",
        "radio",
        "tab",
        "menuitem",
        "heading",
        "row",
        "cell",
        "image",
        "other",
    ] = "other"
    selector: StableSelector
    fallbacks: list[StableSelector] = Field(default_factory=list)
    required: bool = False
    visible: bool = True
    enabled: bool = True


class FormSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    fields: list[str] = Field(default_factory=list)
    submit_id: str | None = None


class PageExtraction(BaseModel):
    """Compact, automation-only snapshot of a single URL."""

    model_config = ConfigDict(extra="forbid")

    url: str
    final_url: str
    title: str
    kind: URLKind
    requires_auth: bool = False
    redirected_to: str | None = None
    elements: list[Element] = Field(default_factory=list)
    forms: list[FormSpec] = Field(default_factory=list)
    headings: list[str] = Field(default_factory=list)
    captured_at: str = Field(default_factory=_now_iso)
    fingerprint: str = ""


class URLNode(BaseModel):
    """Entry in the registry — one row per URL the user gave us."""

    model_config = ConfigDict(extra="forbid")

    url: str
    slug: str
    kind: URLKind = URLKind.UNKNOWN
    requires_auth: bool = False
    depends_on: list[str] = Field(default_factory=list)
    redirects_to: str | None = None
    status: Status = Status.PENDING
    extraction_path: str | None = None
    plan_path: str | None = None
    pom_path: str | None = None
    feature_path: str | None = None
    steps_path: str | None = None
    last_fingerprint: str | None = None
    last_run_at: str | None = None
    notes: list[str] = Field(default_factory=list)


class AuthSpec(BaseModel):
    """How the orchestrator should authenticate before exploring protected URLs."""

    model_config = ConfigDict(extra="forbid")

    login_url: str
    username_env: str = "LOGIN_USERNAME"
    password_env: str = "LOGIN_PASSWORD"
    otp_secret_env: str | None = "LOGIN_OTP_SECRET"
    # ``auth_kind`` tells the runner which flow to execute. The set is
    # open-ended so detection can evolve without schema churn.
    #
    # * ``form``            — username + password inputs inline
    #                         (classic single-step flow).
    # * ``username_first``  — identifier-first: the page shows only a
    #                         username/email and a Next/Continue button.
    #                         The password field (or an IdP) appears
    #                         after clicking Next.
    # * ``email_only``      — magic-link style where the only input is
    #                         an email; submission causes a link/code
    #                         to be sent (completion is external).
    # * ``magic_link``      — explicit "Email me a login link" flow.
    #                         Requires the user to open their inbox.
    # * ``otp_code``        — "Send me a code" flow; code entry may be
    #                         on a second page or via the same email.
    # * ``sso_microsoft``   — provider button that redirects to
    #                         ``login.microsoftonline.com``.
    # * ``sso_generic``     — any other provider button; runner clicks
    #                         and then looks for a standard form at the
    #                         destination.
    # * ``unknown_auth``    — login page shape was not recognised; we
    #                         still emit a best-effort scaffold.
    auth_kind: Literal[
        "form",
        "username_first",
        "email_only",
        "magic_link",
        "otp_code",
        "sso_microsoft",
        "sso_generic",
        "unknown_auth",
    ] = "form"
    username_selector: StableSelector | None = None
    password_selector: StableSelector | None = None
    submit_selector: StableSelector | None = None
    # Second-step "Next"/"Continue"/"Send link"/"Send code" button.
    # Used by multi-step flows (``username_first``, ``email_only``,
    # ``magic_link``, ``otp_code``) to advance past the first screen.
    continue_selector: StableSelector | None = None
    # Selector for the provider button on the app's own login page
    # (only used when ``auth_kind`` starts with ``sso_``).
    sso_button_selector: StableSelector | None = None
    # ``True`` when the flow cannot be completed end-to-end by the
    # orchestrator (magic link in an email, hardware-MFA prompt, etc.).
    # The rest of the pipeline still renders the best scaffold it can
    # and surfaces this flag in the run summary.
    requires_external_completion: bool = False
    success_indicator_url_contains: str | None = None
    success_indicator_text: str | None = None
    setup_path: str | None = None
    storage_state_path: str | None = None
    last_run_at: str | None = None
    status: Status = Status.PENDING
    # Free-text diagnostics from probe/runner. Shown in run summary.
    notes: list[str] = Field(default_factory=list)


class Registry(BaseModel):
    """Top-level manifest persisted as YAML."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    base_url: str = ""
    auth: AuthSpec | None = None
    nodes: dict[str, URLNode] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=_now_iso)


class POMPlan(BaseModel):
    """JSON action plan — POM derivation. Output of one LLM call."""

    model_config = ConfigDict(extra="forbid")

    class_name: str
    fixture_name: str
    methods: list["POMMethod"] = Field(default_factory=list)


class POMMethod(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    intent: str
    element_id: str
    action: Literal["click", "fill", "check", "select", "navigate", "wait", "expect_visible", "expect_text"]
    args: list[str] = Field(default_factory=list)
    returns: str | None = None


POMPlan.model_rebuild()


class FeaturePlan(BaseModel):
    """JSON action plan — Gherkin feature derivation."""

    model_config = ConfigDict(extra="forbid")

    feature: str
    description: str
    background: list["StepRef"] = Field(default_factory=list)
    scenarios: list["ScenarioPlan"] = Field(default_factory=list)


class StepRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: Literal["Given", "When", "Then", "And"]
    text: str
    pom_method: str | None = None
    args: list[str] = Field(default_factory=list)


class ScenarioPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    tier: Literal[
        "smoke",
        "sanity",
        "regression",
        "happy",
        "edge",
        "validation",
        "navigation",
        "auth",
        "rbac",
        "e2e",
    ] = "smoke"
    steps: list[StepRef] = Field(default_factory=list)


FeaturePlan.model_rebuild()
