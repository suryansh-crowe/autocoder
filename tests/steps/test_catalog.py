"""Generated step definitions for 'Catalog page core interactions'."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect
from pytest_bdd import given, parsers, scenarios, then, when

from tests.pages.catalog_page import CatalogPage

scenarios("catalog.feature")


@pytest.fixture
def catalog_page(page: Page) -> CatalogPage:
    return CatalogPage(page)


@given(parsers.parse('User is on the Catalog page'))
def _user_is_on_the_catalog_page(catalog_page: CatalogPage) -> None:
    catalog_page.navigate()


@given(parsers.parse('User enters a valid asset name in the search box'))
def _user_enters_a_valid_asset_name_in_the_search_box(catalog_page: CatalogPage) -> None:
    catalog_page.fill_search_assets('valid asset name')


@when(parsers.parse('User clicks the Filter button'))
def _user_clicks_the_filter_button(catalog_page: CatalogPage) -> None:
    catalog_page.click_filter()


@then(parsers.parse('Pagination controls are displayed'))
def _pagination_controls_are_displayed(catalog_page: CatalogPage) -> None:
    expect(catalog_page.locate('previous')).to_be_visible()


@given(parsers.parse('User leaves the search box empty'))
def _user_leaves_the_search_box_empty(catalog_page: CatalogPage) -> None:
    catalog_page.fill_search_assets('')


@then(parsers.parse('Pagination controls remain unchanged'))
def _pagination_controls_remain_unchanged(catalog_page: CatalogPage) -> None:
    expect(catalog_page.locate('previous')).to_be_visible()


@when(parsers.parse('User clicks the Home button'))
def _user_clicks_the_home_button(catalog_page: CatalogPage) -> None:
    catalog_page.click_home()


@then(parsers.parse('The Catalog heading is visible'))
def _the_catalog_heading_is_visible(catalog_page: CatalogPage) -> None:
    expect(catalog_page.locate('data_catalog')).to_be_visible()


@when(parsers.parse('User clicks the Close sidebar button'))
def _user_clicks_the_close_sidebar_button(catalog_page: CatalogPage) -> None:
    catalog_page.click_close_sidebar()


@when(parsers.parse('User clicks the Ask Stewie button'))
def _user_clicks_the_ask_stewie_button(catalog_page: CatalogPage) -> None:
    catalog_page.click_ask_stewie()


@then(parsers.parse('The Stewie assistant panel is displayed'))
def _the_stewie_assistant_panel_is_displayed(catalog_page: CatalogPage) -> None:
    expect(catalog_page.locate('open_stewie_assistant')).to_be_visible()


@when(parsers.parse('User clicks the Data Quality button'))
def _user_clicks_the_data_quality_button(catalog_page: CatalogPage) -> None:
    catalog_page.click_data_quality()
