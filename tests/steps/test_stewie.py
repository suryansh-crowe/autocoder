"""Generated step definitions for 'Stewie AI chat prompt submission, role selection, and cross-page navigation'."""

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


@given(parsers.parse('The user is on the Stewie AI page'))
def _the_user_is_on_the_stewie_ai_page(stewie_page: StewiePage) -> None:
    stewie_page.navigate()


@when(parsers.parse("The user enters 'List all assets within Finance domain.' in the chat prompt textbox"))
def _the_user_enters_list_all_assets_within_finance_dom(stewie_page: StewiePage) -> None:
    stewie_page.fill_ask_stewie_type_for_asset_suggestions('List all assets within Finance domain.')


@when(parsers.parse('The user clicks the Send button'))
def _the_user_clicks_the_send_button(stewie_page: StewiePage) -> None:
    stewie_page.click_send()


@then(parsers.parse('A new conversation thread appears'))
def _a_new_conversation_thread_appears(stewie_page: StewiePage) -> None:
    raise NotImplementedError('Implement step: A new conversation thread appears')


@when(parsers.parse('The user clears the chat prompt textbox'))
def _the_user_clears_the_chat_prompt_textbox(stewie_page: StewiePage) -> None:
    stewie_page.locate('ask_stewie_type_for_asset_suggestions').fill('')


@then(parsers.parse('The Send button becomes disabled'))
def _the_send_button_becomes_disabled(stewie_page: StewiePage) -> None:
    expect(stewie_page.locate('send')).to_be_disabled()


@when(parsers.parse("The user selects 'Data Analyst' from the Choose role dropdown"))
def _the_user_selects_data_analyst_from_the_choose_role(stewie_page: StewiePage) -> None:
    stewie_page.select_choose_role('Data Analyst')


@then(parsers.parse('The chat prompt textbox becomes enabled'))
def _the_chat_prompt_textbox_becomes_enabled(stewie_page: StewiePage) -> None:
    expect(stewie_page.locate('ask_stewie_type_for_asset_suggestions')).to_be_enabled()


@when(parsers.parse('The user clicks the Home button'))
def _the_user_clicks_the_home_button(stewie_page: StewiePage) -> None:
    stewie_page.click_home()


@then(parsers.parse("The URL contains '/home'"))
def _the_url_contains_home(stewie_page: StewiePage) -> None:
    expect(stewie_page.page).to_have_url(re.compile('/home(?:[/?#]|$)'))


@then(parsers.parse("The 'Stewie Terminal' heading is displayed"))
def _the_stewie_terminal_heading_is_displayed(stewie_page: StewiePage) -> None:
    expect(stewie_page.page.get_by_role('heading', name='Stewie Terminal')).to_be_visible()
