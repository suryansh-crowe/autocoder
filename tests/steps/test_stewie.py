"""Generated step definitions for 'Stewie AI main page interaction'."""

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


@given(parsers.parse('User is on the Stewie AI main page'))
def _user_is_on_the_stewie_ai_main_page(stewie_page: StewiePage) -> None:
    stewie_page.navigate()


@given(parsers.parse('User enters a prompt in the Ask Stewie message box'))
def _user_enters_a_prompt_in_the_ask_stewie_message_box(stewie_page: StewiePage) -> None:
    stewie_page.fill_ask_stewie_type_for_asset_suggestions('example prompt')


@when(parsers.parse('User clicks the Send button'))
def _user_clicks_the_send_button(stewie_page: StewiePage) -> None:
    stewie_page.click_send()


@then(parsers.parse('The Conversations panel is displayed'))
def _the_conversations_panel_is_displayed(stewie_page: StewiePage) -> None:
    expect(stewie_page.locate('new_conversation')).to_be_visible()


@when(parsers.parse('User clicks the Data Catalog button'))
def _user_clicks_the_data_catalog_button(stewie_page: StewiePage) -> None:
    stewie_page.click_data_catalog()


@then(parsers.parse('The Data Catalog section is visible'))
def _the_data_catalog_section_is_visible(stewie_page: StewiePage) -> None:
    expect(stewie_page.locate('data_quality')).to_be_visible()


@when(parsers.parse('User clicks the Ask Stewie button'))
def _user_clicks_the_ask_stewie_button(stewie_page: StewiePage) -> None:
    stewie_page.click_ask_stewie()


@then(parsers.parse('The Ask Stewie message box is visible'))
def _the_ask_stewie_message_box_is_visible(stewie_page: StewiePage) -> None:
    expect(stewie_page.locate('ask_stewie_type_for_asset_suggestions')).to_be_visible()


@when(parsers.parse('User selects a role from the Choose role dropdown'))
def _user_selects_a_role_from_the_choose_role_dropdown(stewie_page: StewiePage) -> None:
    stewie_page.select_choose_role('value')


@then(parsers.parse('The Send button is enabled'))
def _the_send_button_is_enabled(stewie_page: StewiePage) -> None:
    expect(stewie_page.locate('send')).to_be_enabled()


@when(parsers.parse('User clicks the Send button without selecting a role'))
def _user_clicks_the_send_button_without_selecting_a_ro(stewie_page: StewiePage) -> None:
    stewie_page.click_send()


@then(parsers.parse('A validation message is displayed for role selection'))
def _a_validation_message_is_displayed_for_role_selecti(stewie_page: StewiePage) -> None:
    expect(stewie_page.locate('choose_role')).to_be_visible()


@when(parsers.parse('User clicks the Notifications button'))
def _user_clicks_the_notifications_button(stewie_page: StewiePage) -> None:
    stewie_page.click_notifications()


@then(parsers.parse('The Notifications panel is visible'))
def _the_notifications_panel_is_visible(stewie_page: StewiePage) -> None:
    pass
