"""Generated step definitions for 'Stewie AI Agent Page Core Interactions'."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect
from pytest_bdd import given, parsers, scenarios, then, when

from tests.pages.agent_page import AgentPage

scenarios("agent.feature")


@pytest.fixture
def agent_page(page: Page) -> AgentPage:
    return AgentPage(page)


@given(parsers.parse('User is on the Stewie AI Agents page'))
def _user_is_on_the_stewie_ai_agents_page(agent_page: AgentPage) -> None:
    agent_page.navigate()


@when(parsers.parse('User enters a valid agent name in the search box'))
def _user_enters_a_valid_agent_name_in_the_search_box(agent_page: AgentPage) -> None:
    agent_page.fill_search_agents(Data Quality Agent)


@then(parsers.parse('The Data Quality Agent heading is displayed'))
def _the_data_quality_agent_heading_is_displayed(agent_page: AgentPage) -> None:
    expect(agent_page.locate('data_quality')).to_be_visible()


@when(parsers.parse('User submits the search box with no input'))
def _user_submits_the_search_box_with_no_input(agent_page: AgentPage) -> None:
    agent_page.fill_search_agents()


@then(parsers.parse('The Agents heading remains visible'))
def _the_agents_heading_remains_visible(agent_page: AgentPage) -> None:
    expect(agent_page.locate('agents')).to_be_visible()


@when(parsers.parse('User clicks the Home button'))
def _user_clicks_the_home_button(agent_page: AgentPage) -> None:
    agent_page.click_home()


@then(parsers.parse('The Home page heading is displayed'))
def _the_home_page_heading_is_displayed(agent_page: AgentPage) -> None:
    raise NotImplementedError("Implement step: The Home page heading is displayed")


@when(parsers.parse('User clicks the Close sidebar button'))
def _user_clicks_the_close_sidebar_button(agent_page: AgentPage) -> None:
    agent_page.click_close_sidebar()


@then(parsers.parse('The sidebar is no longer visible'))
def _the_sidebar_is_no_longer_visible(agent_page: AgentPage) -> None:
    raise NotImplementedError("Implement step: The sidebar is no longer visible")


@when(parsers.parse('User clicks the Ask Stewie button'))
def _user_clicks_the_ask_stewie_button(agent_page: AgentPage) -> None:
    agent_page.click_ask_stewie()


@then(parsers.parse('The Ask Stewie message box is visible'))
def _the_ask_stewie_message_box_is_visible(agent_page: AgentPage) -> None:
    expect(agent_page.locate('search_agents')).to_be_visible()


@when(parsers.parse('User clicks the Data Catalog button'))
def _user_clicks_the_data_catalog_button(agent_page: AgentPage) -> None:
    agent_page.click_data_catalog()


@then(parsers.parse('The Data Catalog section heading is displayed'))
def _the_data_catalog_section_heading_is_displayed(agent_page: AgentPage) -> None:
    expect(agent_page.locate('data_quality')).to_be_visible()
