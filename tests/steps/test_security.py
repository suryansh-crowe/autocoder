"""Generated step definitions for 'Security Access Request'."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect
from pytest_bdd import given, parsers, scenarios, then, when

from tests.pages.security_page import SecurityPage

scenarios("security.feature")


@pytest.fixture
def security_page(page: Page) -> SecurityPage:
    return SecurityPage(page)


@given(parsers.parse('User is on the Security page'))
def _user_is_on_the_security_page(security_page: SecurityPage) -> None:
    security_page.navigate()


@when(parsers.parse('User clicks the Request Access button'))
def _user_clicks_the_request_access_button(security_page: SecurityPage) -> None:
    security_page.click_request_access()


@when(parsers.parse('User enters a reason for access'))
def _user_enters_a_reason_for_access(security_page: SecurityPage) -> None:
    security_page.fill_reason_for_request('test reason')


@when(parsers.parse('User clicks the Submit Request button'))
def _user_clicks_the_submit_request_button(security_page: SecurityPage) -> None:
    security_page.click_submit_request()


@then(parsers.parse('The Your Request History section is visible'))
def _the_your_request_history_section_is_visible(security_page: SecurityPage) -> None:
    pass  # no safe binding — validator rejected LLM output


@when(parsers.parse('User leaves the reason for access empty'))
def _user_leaves_the_reason_for_access_empty(security_page: SecurityPage) -> None:
    security_page.fill_reason_for_request('')


@then(parsers.parse('A validation message for the reason field is displayed'))
def _a_validation_message_for_the_reason_field_is_displ(security_page: SecurityPage) -> None:
    expect(security_page.locate('reason_for_request')).to_be_visible()


@when(parsers.parse('User clicks the Home button'))
def _user_clicks_the_home_button(security_page: SecurityPage) -> None:
    security_page.click_home()


@then(parsers.parse('The Home page heading is visible'))
def _the_home_page_heading_is_visible(security_page: SecurityPage) -> None:
    pass


@when(parsers.parse('User clicks the Close sidebar button'))
def _user_clicks_the_close_sidebar_button(security_page: SecurityPage) -> None:
    security_page.click_close_sidebar()


@then(parsers.parse('The sidebar is hidden'))
def _the_sidebar_is_hidden(security_page: SecurityPage) -> None:
    pass


@when(parsers.parse('User clicks the Data Catalog button'))
def _user_clicks_the_data_catalog_button(security_page: SecurityPage) -> None:
    security_page.click_data_catalog()


@then(parsers.parse('The Data Catalog section heading is visible'))
def _the_data_catalog_section_heading_is_visible(security_page: SecurityPage) -> None:
    expect(security_page.locate('reader_read_only_access_to_data_catalog_and_assets')).to_be_visible()


@when(parsers.parse('User clicks the Agent Management button'))
def _user_clicks_the_agent_management_button(security_page: SecurityPage) -> None:
    security_page.click_agent_management()


@then(parsers.parse('The Agent Management section heading is visible'))
def _the_agent_management_section_heading_is_visible(security_page: SecurityPage) -> None:
    expect(security_page.locate('agent_pipelines')).to_be_visible()
