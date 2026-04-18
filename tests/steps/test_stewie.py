"""Generated step definitions for 'Stewie AI Sign-In and Terms Navigation'."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect
from pytest_bdd import given, parsers, scenarios, then, when

from tests.pages.stewie_page import StewiePage

scenarios("stewie.feature")


@pytest.fixture
def stewie_page(page: Page) -> StewiePage:
    return StewiePage(page)


@given(parsers.parse('I am on the Stewie AI homepage'))
def _i_am_on_the_stewie_ai_homepage(stewie_page: StewiePage) -> None:
    raise NotImplementedError("Implement step: I am on the Stewie AI homepage")


@when(parsers.parse('I click the sign-in button for Microsoft'))
def _i_click_the_sign_in_button_for_microsoft(stewie_page: StewiePage) -> None:
    stewie_page.click_sign_in_with_microsoft()


@when(parsers.parse('I fill in my email address'))
def _i_fill_in_my_email_address(stewie_page: StewiePage) -> None:
    raise NotImplementedError("Implement step: I fill in my email address (POM method 'fill_email' expects: value)")


@when(parsers.parse('I click the terms of service button'))
def _i_click_the_terms_of_service_button(stewie_page: StewiePage) -> None:
    stewie_page.click_terms_of_service()


@when(parsers.parse('I click the privacy policy button'))
def _i_click_the_privacy_policy_button(stewie_page: StewiePage) -> None:
    stewie_page.click_privacy_policy()


@then(parsers.parse('The terms of service button is visible'))
def _the_terms_of_service_button_is_visible(stewie_page: StewiePage) -> None:
    raise NotImplementedError("Implement step: The terms of service button is visible")


@then(parsers.parse('The privacy policy button is visible'))
def _the_privacy_policy_button_is_visible(stewie_page: StewiePage) -> None:
    raise NotImplementedError("Implement step: The privacy policy button is visible")
