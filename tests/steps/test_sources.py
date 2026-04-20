"""Generated step definitions for 'Stewie AI Sources Page Navigation and Actions'."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect
from pytest_bdd import given, parsers, scenarios, then, when

from tests.pages.sources_page import SourcesPage

scenarios("sources.feature")


@pytest.fixture
def sources_page(page: Page) -> SourcesPage:
    return SourcesPage(page)


@given(parsers.parse('User is on the Stewie AI Sources page'))
def _user_is_on_the_stewie_ai_sources_page(sources_page: SourcesPage) -> None:
    sources_page.navigate()


@when(parsers.parse('User clicks the Home button'))
def _user_clicks_the_home_button(sources_page: SourcesPage) -> None:
    sources_page.click_home()


@then(parsers.parse('The Recent Pipelines heading is visible'))
def _the_recent_pipelines_heading_is_visible(sources_page: SourcesPage) -> None:
    expect(sources_page.locate('agent_pipelines')).to_be_visible()


@when(parsers.parse('User clicks the Ask Stewie button'))
def _user_clicks_the_ask_stewie_button(sources_page: SourcesPage) -> None:
    sources_page.click_ask_stewie()


@then(parsers.parse('The Stewie chat panel is displayed'))
def _the_stewie_chat_panel_is_displayed(sources_page: SourcesPage) -> None:
    pass


@when(parsers.parse('User clicks the Data Catalog button'))
def _user_clicks_the_data_catalog_button(sources_page: SourcesPage) -> None:
    sources_page.click_data_catalog()


@then(parsers.parse('The Pipeline Status heading is visible'))
def _the_pipeline_status_heading_is_visible(sources_page: SourcesPage) -> None:
    expect(sources_page.locate('agent_pipelines')).to_be_visible()


@when(parsers.parse('User clicks the Source Connection button'))
def _user_clicks_the_source_connection_button(sources_page: SourcesPage) -> None:
    sources_page.click_source_connection()


@then(parsers.parse('The Execution Logs heading is visible'))
def _the_execution_logs_heading_is_visible(sources_page: SourcesPage) -> None:
    expect(sources_page.locate('runs_logs')).to_be_visible()


@when(parsers.parse('User clicks the Notifications button'))
def _user_clicks_the_notifications_button(sources_page: SourcesPage) -> None:
    sources_page.click_notifications()


@then(parsers.parse('The notification panel is displayed'))
def _the_notification_panel_is_displayed(sources_page: SourcesPage) -> None:
    pass
