"""Generated step definitions for 'Stewie AI Home Page Core Interactions'."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect
from pytest_bdd import given, parsers, scenarios, then, when

from tests.pages.home_page import HomePage

scenarios("home.feature")


@pytest.fixture
def home_page(page: Page) -> HomePage:
    return HomePage(page)


@given(parsers.parse('User is on the Stewie AI home page'))
def _user_is_on_the_stewie_ai_home_page(home_page: HomePage) -> None:
    home_page.navigate()


@when(parsers.parse('User enters a valid asset search query'))
def _user_enters_a_valid_asset_search_query(home_page: HomePage) -> None:
    home_page.fill_search_assets('valid asset search query')


@when(parsers.parse('User submits the asset search'))
def _user_submits_the_asset_search(home_page: HomePage) -> None:
    home_page.click_view_all()


@then(parsers.parse('The My Domain Health section is updated with search results'))
def _the_my_domain_health_section_is_updated_with_searc(home_page: HomePage) -> None:
    expect(home_page.locate('html_1_body_1_div_1_div_2_div_2_div_1_div_2_div_1_div_1_div_1_div_3_section_2_div_1_div_1_button_1')).to_be_visible()


@when(parsers.parse('User submits the asset search with no input'))
def _user_submits_the_asset_search_with_no_input(home_page: HomePage) -> None:
    home_page.click_view_all()


@then(parsers.parse('A validation message is displayed for the search box'))
def _a_validation_message_is_displayed_for_the_search_b(home_page: HomePage) -> None:
    expect(home_page.locate('ask_stewie_about_assets_rules_domains')).to_be_visible()


@when(parsers.parse('User types a question in the Ask Stewie chat box'))
def _user_types_a_question_in_the_ask_stewie_chat_box(home_page: HomePage) -> None:
    home_page.fill_ask_stewie_about_assets('question')


@when(parsers.parse('User clicks the Ask button'))
def _user_clicks_the_ask_button(home_page: HomePage) -> None:
    home_page.click_ask()


@then(parsers.parse('The Ask Stewie chat panel displays the response'))
def _the_ask_stewie_chat_panel_displays_the_response(home_page: HomePage) -> None:
    expect(home_page.locate('ask_stewie')).to_be_visible()


@when(parsers.parse('User clicks the Data Catalog navigation button'))
def _user_clicks_the_data_catalog_navigation_button(home_page: HomePage) -> None:
    home_page.click_data_catalog()


@then(parsers.parse('The Stewie Terminal heading is visible'))
def _the_stewie_terminal_heading_is_visible(home_page: HomePage) -> None:
    expect(home_page.locate('ask_stewie')).to_be_visible()


@when(parsers.parse('User clicks the Data Quality action button'))
def _user_clicks_the_data_quality_action_button(home_page: HomePage) -> None:
    home_page.click_data_quality()


@then(parsers.parse('The Admin Actions section is displayed'))
def _the_admin_actions_section_is_displayed(home_page: HomePage) -> None:
    expect(home_page.locate('html_1_body_1_div_1_div_2_div_2_div_1_div_2_div_1_div_1_div_1_div_3_section_2_div_1_div_1_button_1')).to_be_visible()
