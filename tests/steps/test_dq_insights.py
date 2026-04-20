"""Generated step definitions for 'Stewie AI Insights dashboard interaction'."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect
from pytest_bdd import given, parsers, scenarios, then, when

from tests.pages.dq_insights_page import DqInsightsPage

scenarios("dq_insights.feature")


@pytest.fixture
def dq_insights_page(page: Page) -> DqInsightsPage:
    return DqInsightsPage(page)


@given(parsers.parse('User is on the Data Quality Insights page'))
def _user_is_on_the_data_quality_insights_page(dq_insights_page: DqInsightsPage) -> None:
    dq_insights_page.navigate()


@when(parsers.parse('User enters a dashboard name in the search box'))
def _user_enters_a_dashboard_name_in_the_search_box(dq_insights_page: DqInsightsPage) -> None:
    dq_insights_page.fill_search_dashboard('dashboard name')


@when(parsers.parse('User clicks the Filter button to submit the search'))
def _user_clicks_the_filter_button_to_submit_the_search(dq_insights_page: DqInsightsPage) -> None:
    pass


@then(parsers.parse('The Dashboards heading is visible indicating search results'))
def _the_dashboards_heading_is_visible_indicating_searc(dq_insights_page: DqInsightsPage) -> None:
    expect(dq_insights_page.locate('dashboards')).to_be_visible()


@when(parsers.parse('User leaves the search box empty'))
def _user_leaves_the_search_box_empty(dq_insights_page: DqInsightsPage) -> None:
    dq_insights_page.fill_search_dashboard('')


@when(parsers.parse('User clicks the Filter button'))
def _user_clicks_the_filter_button(dq_insights_page: DqInsightsPage) -> None:
    pass


@then(parsers.parse('The Dashboards heading remains visible and no validation message appears'))
def _the_dashboards_heading_remains_visible_and_no_vali(dq_insights_page: DqInsightsPage) -> None:
    expect(dq_insights_page.locate('notifications')).to_be_visible()


@when(parsers.parse('User clicks the Dashboards navigation button'))
def _user_clicks_the_dashboards_navigation_button(dq_insights_page: DqInsightsPage) -> None:
    dq_insights_page.click_dashboards()


@then(parsers.parse('The Dashboards heading is displayed'))
def _the_dashboards_heading_is_displayed(dq_insights_page: DqInsightsPage) -> None:
    expect(dq_insights_page.locate('public_dashboards_4')).to_be_visible()


@when(parsers.parse('User clicks the Ask Stewie button'))
def _user_clicks_the_ask_stewie_button(dq_insights_page: DqInsightsPage) -> None:
    dq_insights_page.click_ask_stewie()


@then(parsers.parse('The Ask Stewie interface is displayed'))
def _the_ask_stewie_interface_is_displayed(dq_insights_page: DqInsightsPage) -> None:
    pass


@when(parsers.parse('User clicks the Data Catalog button'))
def _user_clicks_the_data_catalog_button(dq_insights_page: DqInsightsPage) -> None:
    dq_insights_page.click_data_catalog()


@then(parsers.parse('The Data Quality heading is visible'))
def _the_data_quality_heading_is_visible(dq_insights_page: DqInsightsPage) -> None:
    expect(dq_insights_page.locate('data_quality')).to_be_visible()
