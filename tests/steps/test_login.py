"""Generated step definitions for 'User Login and Agreement to Terms'."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect
from pytest_bdd import given, parsers, scenarios, then, when

from tests.pages.login_page import LoginPage

scenarios("login.feature")


@pytest.fixture
def login_page(page: Page) -> LoginPage:
    return LoginPage(page)


@given(parsers.parse('I am on the login page'))
def _i_am_on_the_login_page(login_page: LoginPage) -> None:
    login_page.navigate()


@when(parsers.parse('I click sign in with Microsoft'))
def _i_click_sign_in_with_microsoft(login_page: LoginPage) -> None:
    login_page.click_sign_in_with_microsoft()


@then(parsers.parse('I should be logged in successfully'))
def _i_should_be_logged_in_successfully(login_page: LoginPage) -> None:
    expect(login_page.page).to_have_url('https://aps-aitl-frontend-bja4eebjg6cyguea.northcentralus-01.azurewebsites.net/login')


@when(parsers.parse('I click terms of service'))
def _i_click_terms_of_service(login_page: LoginPage) -> None:
    login_page.click_terms_of_service()


@when(parsers.parse('I click privacy policy'))
def _i_click_privacy_policy(login_page: LoginPage) -> None:
    login_page.click_privacy_policy()


@when(parsers.parse('I fill in my email address'))
def _i_fill_in_my_email_address(login_page: LoginPage) -> None:
    login_page.fill_email('your-email@example.com')
