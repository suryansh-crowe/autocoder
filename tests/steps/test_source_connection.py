"""Generated step definitions for 'Source Connection navigation and action coverage'."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect
from pytest_bdd import given, parsers, scenarios, then, when

from tests.pages.source_connection_page import SourceConnectionPage

scenarios("source_connection.feature")


@pytest.fixture
def source_connection_page(page: Page) -> SourceConnectionPage:
    return SourceConnectionPage(page)


@given(parsers.parse('User is on the Source Connection page'))
def _user_is_on_the_source_connection_page(source_connection_page: SourceConnectionPage) -> None:
    source_connection_page.navigate()


@when(parsers.parse('User clicks the Home button'))
def _user_clicks_the_home_button(source_connection_page: SourceConnectionPage) -> None:
    source_connection_page.click_home()


@then(parsers.parse('The Source Connection heading is visible'))
def _the_source_connection_heading_is_visible(source_connection_page: SourceConnectionPage) -> None:
    expect(source_connection_page.locate('source_connection')).to_be_visible()


@when(parsers.parse('User clicks the Close sidebar button'))
def _user_clicks_the_close_sidebar_button(source_connection_page: SourceConnectionPage) -> None:
    source_connection_page.click_close_sidebar()


@then(parsers.parse('The sidebar is no longer visible'))
def _the_sidebar_is_no_longer_visible(source_connection_page: SourceConnectionPage) -> None:
    pass


@when(parsers.parse('User clicks the Oracle Database button'))
def _user_clicks_the_oracle_database_button(source_connection_page: SourceConnectionPage) -> None:
    source_connection_page.click_oracle_database()


@then(parsers.parse('The Connect a Data Source heading is visible'))
def _the_connect_a_data_source_heading_is_visible(source_connection_page: SourceConnectionPage) -> None:
    expect(source_connection_page.locate('source_connection')).to_be_visible()


@when(parsers.parse('User clicks the Ask Stewie button'))
def _user_clicks_the_ask_stewie_button(source_connection_page: SourceConnectionPage) -> None:
    source_connection_page.click_ask_stewie()


@then(parsers.parse('The Stewie AI interface is displayed'))
def _the_stewie_ai_interface_is_displayed(source_connection_page: SourceConnectionPage) -> None:
    expect(source_connection_page.locate('source_connection')).to_be_visible()


@when(parsers.parse('User clicks the Data Catalog button'))
def _user_clicks_the_data_catalog_button(source_connection_page: SourceConnectionPage) -> None:
    source_connection_page.click_data_catalog()


@then(parsers.parse('The catalog section is visible'))
def _the_catalog_section_is_visible(source_connection_page: SourceConnectionPage) -> None:
    expect(source_connection_page.locate('catalog_test_db_tesgpqj_nq15615')).to_be_visible()
