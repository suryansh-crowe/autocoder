"""Generated step definitions for 'Stewie AI Home: Search, Chat, Navigation, and Action Consequences'."""

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


@given(parsers.parse('The user is on the Stewie AI Home page'))
def _the_user_is_on_the_stewie_ai_home_page(home_page: HomePage) -> None:
    home_page.navigate()


@when(parsers.parse("The user enters 'DQ-AGT-001' in the asset search box"))
def _the_user_enters_dq_agt_001_in_the_asset_search_box(home_page: HomePage) -> None:
    home_page.fill_search_for_assets_terms_rules('DQ-AGT-001')


@when(parsers.parse('The user submits the asset search'))
def _the_user_submits_the_asset_search(home_page: HomePage) -> None:
    home_page.submit_search_for_assets_terms_rules()


@then(parsers.parse("The results area contains 'DQ-AGT-001'"))
def _the_results_area_contains_dq_agt_001(home_page: HomePage) -> None:
    expect(home_page.page.get_by_text('DQ-AGT-001', exact=False)).to_be_visible()


@when(parsers.parse("The user enters 'unknown_id_42' in the asset search box"))
def _the_user_enters_unknown_id_42_in_the_asset_search_(home_page: HomePage) -> None:
    home_page.fill_search_for_assets_terms_rules('unknown_id_42')


@then(parsers.parse("The results area displays 'No matching assets'"))
def _the_results_area_displays_no_matching_assets(home_page: HomePage) -> None:
    expect(home_page.page.get_by_text('No matching assets', exact=False)).to_be_visible()


@when(parsers.parse('The user clears the asset search box'))
def _the_user_clears_the_asset_search_box(home_page: HomePage) -> None:
    home_page.fill_search_for_assets_terms_rules('')


@then(parsers.parse('The results area shows the same number of rows as before'))
def _the_results_area_shows_the_same_number_of_rows_as_(home_page: HomePage) -> None:
    raise NotImplementedError('Implement step: The results area shows the same number of rows as before')


@when(parsers.parse("The user enters 'Show asset rules for DQ-AGT-001' in the Ask Stewie chat box"))
def _the_user_enters_show_asset_rules_for_dq_agt_001_in(home_page: HomePage) -> None:
    home_page.fill_ask_stewie_about_assets_rules_domains('Show asset rules for DQ-AGT-001')


@when(parsers.parse('The user submits the chat prompt'))
def _the_user_submits_the_chat_prompt(home_page: HomePage) -> None:
    home_page.submit_ask_stewie_about_assets_rules_domains()


@then(parsers.parse("The chat response area displays a message containing 'DQ-AGT-001'"))
def _the_chat_response_area_displays_a_message_containi(home_page: HomePage) -> None:
    expect(home_page.page.get_by_text('DQ-AGT-001', exact=False)).to_be_visible()


@when(parsers.parse('The user clears the Ask Stewie chat box'))
def _the_user_clears_the_ask_stewie_chat_box(home_page: HomePage) -> None:
    home_page.fill_ask_stewie_about_assets_rules_domains('')


@then(parsers.parse('A validation error message appears in the chat area'))
def _a_validation_error_message_appears_in_the_chat_are(home_page: HomePage) -> None:
    raise NotImplementedError('Implement step: A validation error message appears in the chat area')


@when(parsers.parse('The user clicks the Data Catalog button'))
def _the_user_clicks_the_data_catalog_button(home_page: HomePage) -> None:
    home_page.click_data_catalog()


@then(parsers.parse("The URL contains '/catalog'"))
def _the_url_contains_catalog(home_page: HomePage) -> None:
    expect(home_page.page).to_have_url(re.compile('/catalog(?:[/?#]|$)'))


@then(parsers.parse('The Catalog page landmark is visible'))
def _the_catalog_page_landmark_is_visible(home_page: HomePage) -> None:
    expect(home_page.page.get_by_text('Catalog', exact=False)).to_be_visible()


@when(parsers.parse('The user clicks the Ask Stewie button'))
def _the_user_clicks_the_ask_stewie_button(home_page: HomePage) -> None:
    home_page.click_ask_stewie()


@then(parsers.parse("The URL contains '/stewie'"))
def _the_url_contains_stewie(home_page: HomePage) -> None:
    expect(home_page.page).to_have_url(re.compile('/stewie(?:[/?#]|$)'))


@then(parsers.parse("The 'Stewie Terminal' heading is displayed"))
def _the_stewie_terminal_heading_is_displayed(home_page: HomePage) -> None:
    expect(home_page.page.get_by_role('heading', name='Stewie Terminal')).to_be_visible()


@when(parsers.parse('The user clicks the Source Connection button'))
def _the_user_clicks_the_source_connection_button(home_page: HomePage) -> None:
    home_page.click_source_connection()


@then(parsers.parse('The Source Connection page landmark is visible'))
def _the_source_connection_page_landmark_is_visible(home_page: HomePage) -> None:
    raise NotImplementedError('Implement step: The Source Connection page landmark is visible')


@when(parsers.parse('The user clicks the Data Quality button'))
def _the_user_clicks_the_data_quality_button(home_page: HomePage) -> None:
    home_page.click_data_quality()


@then(parsers.parse('The Data Quality page landmark is visible'))
def _the_data_quality_page_landmark_is_visible(home_page: HomePage) -> None:
    raise NotImplementedError('Implement step: The Data Quality page landmark is visible')


@when(parsers.parse('The user clicks the Agent Pipelines button'))
def _the_user_clicks_the_agent_pipelines_button(home_page: HomePage) -> None:
    home_page.click_agent_pipelines()


@then(parsers.parse('The Agent Pipelines page landmark is visible'))
def _the_agent_pipelines_page_landmark_is_visible(home_page: HomePage) -> None:
    raise NotImplementedError('Implement step: The Agent Pipelines page landmark is visible')


@when(parsers.parse('The user clicks the Agent Management button'))
def _the_user_clicks_the_agent_management_button(home_page: HomePage) -> None:
    home_page.click_agent_management()


@then(parsers.parse('The Agent Management page landmark is visible'))
def _the_agent_management_page_landmark_is_visible(home_page: HomePage) -> None:
    raise NotImplementedError('Implement step: The Agent Management page landmark is visible')


@when(parsers.parse('The user clicks the Security button'))
def _the_user_clicks_the_security_button(home_page: HomePage) -> None:
    home_page.click_security()


@then(parsers.parse('The Security page landmark is visible'))
def _the_security_page_landmark_is_visible(home_page: HomePage) -> None:
    raise NotImplementedError('Implement step: The Security page landmark is visible')


@when(parsers.parse('The user clicks the Notifications button'))
def _the_user_clicks_the_notifications_button(home_page: HomePage) -> None:
    home_page.click_notifications()


@then(parsers.parse('The Notifications page landmark is visible'))
def _the_notifications_page_landmark_is_visible(home_page: HomePage) -> None:
    raise NotImplementedError('Implement step: The Notifications page landmark is visible')


@when(parsers.parse('The user clicks the Close sidebar button'))
def _the_user_clicks_the_close_sidebar_button(home_page: HomePage) -> None:
    home_page.click_close_sidebar()


@then(parsers.parse('The main content area becomes fully enabled'))
def _the_main_content_area_becomes_fully_enabled(home_page: HomePage) -> None:
    raise NotImplementedError('Implement step: The main content area becomes fully enabled')
