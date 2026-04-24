"""Generated step definitions for 'Catalog page search, filter, pagination, and navigation'."""

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


@given(parsers.parse('The user is on the Catalog page'))
def _the_user_is_on_the_catalog_page(catalog_page: CatalogPage) -> None:
    catalog_page.navigate()


@when(parsers.parse("The user enters 'DQ-AGT-001' in the asset search box"))
def _the_user_enters_dq_agt_001_in_the_asset_search_box(catalog_page: CatalogPage) -> None:
    catalog_page.fill_search_assets('DQ-AGT-001')


@when(parsers.parse('The user submits the asset search'))
def _the_user_submits_the_asset_search(catalog_page: CatalogPage) -> None:
    catalog_page.submit_search_assets()


@then(parsers.parse("The first results row contains 'DQ-AGT-001'"))
def _the_first_results_row_contains_dq_agt_001(catalog_page: CatalogPage) -> None:
    expect(catalog_page.page.get_by_text('DQ-AGT-001', exact=False)).to_be_visible()


@when(parsers.parse("The user enters 'zzzz_nonexistent_asset' in the asset search box"))
def _the_user_enters_zzzz_nonexistent_asset_in_the_asse(catalog_page: CatalogPage) -> None:
    catalog_page.fill_search_assets('zzzz_nonexistent_asset')


@then(parsers.parse("The results area shows 'No matching assets'"))
def _the_results_area_shows_no_matching_assets(catalog_page: CatalogPage) -> None:
    expect(catalog_page.page.get_by_text('No matching assets', exact=False)).to_be_visible()


@when(parsers.parse('The user clears the asset search box'))
def _the_user_clears_the_asset_search_box(catalog_page: CatalogPage) -> None:
    catalog_page.fill_search_assets('')


@then(parsers.parse('The results row count remains the same as before search'))
def _the_results_row_count_remains_the_same_as_before_s(catalog_page: CatalogPage) -> None:
    raise NotImplementedError('Implement step: The results row count remains the same as before search')


@when(parsers.parse('The user clicks the Filter button'))
def _the_user_clicks_the_filter_button(catalog_page: CatalogPage) -> None:
    catalog_page.click_filter()


@then(parsers.parse('The filter panel becomes visible'))
def _the_filter_panel_becomes_visible(catalog_page: CatalogPage) -> None:
    raise NotImplementedError('Implement step: The filter panel becomes visible')


@when(parsers.parse('The user clicks the Next pagination button'))
def _the_user_clicks_the_next_pagination_button(catalog_page: CatalogPage) -> None:
    catalog_page.click_next_page()


@then(parsers.parse("The pagination indicator reads 'Page 2'"))
def _the_pagination_indicator_reads_page_2(catalog_page: CatalogPage) -> None:
    expect(catalog_page.page.get_by_text('Page 2', exact=False)).to_be_visible()


@when(parsers.parse('The user clicks the Previous pagination button'))
def _the_user_clicks_the_previous_pagination_button(catalog_page: CatalogPage) -> None:
    catalog_page.click_previous_page()


@then(parsers.parse("The pagination indicator reads 'Page 1'"))
def _the_pagination_indicator_reads_page_1(catalog_page: CatalogPage) -> None:
    expect(catalog_page.page.get_by_text('Page 1', exact=False)).to_be_visible()


@when(parsers.parse('The user clicks pagination page 192'))
def _the_user_clicks_pagination_page_192(catalog_page: CatalogPage) -> None:
    catalog_page.click_page_192()


@then(parsers.parse('The Next pagination button becomes disabled'))
def _the_next_pagination_button_becomes_disabled(catalog_page: CatalogPage) -> None:
    expect(catalog_page.locate('next')).to_be_disabled()


@when(parsers.parse('The user clicks the Home button'))
def _the_user_clicks_the_home_button(catalog_page: CatalogPage) -> None:
    catalog_page.click_home()


@then(parsers.parse("The URL contains '/home'"))
def _the_url_contains_home(catalog_page: CatalogPage) -> None:
    expect(catalog_page.page).to_have_url(re.compile('/home(?:[/?#]|$)'))


@then(parsers.parse('The Home page landmark is visible'))
def _the_home_page_landmark_is_visible(catalog_page: CatalogPage) -> None:
    expect(catalog_page.page.get_by_text('Stewie Terminal', exact=False)).to_be_visible()


@when(parsers.parse('The user clicks the Ask Stewie button'))
def _the_user_clicks_the_ask_stewie_button(catalog_page: CatalogPage) -> None:
    catalog_page.click_ask_stewie()


@then(parsers.parse("The URL contains '/stewie'"))
def _the_url_contains_stewie(catalog_page: CatalogPage) -> None:
    expect(catalog_page.page).to_have_url(re.compile('/stewie(?:[/?#]|$)'))


@then(parsers.parse('The Stewie Assistant landmark is visible'))
def _the_stewie_assistant_landmark_is_visible(catalog_page: CatalogPage) -> None:
    expect(catalog_page.page.get_by_text('Conversations', exact=False)).to_be_visible()
