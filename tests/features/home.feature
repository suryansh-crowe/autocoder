Feature: Stewie AI Home Page Core Interactions
  Covers search, chat, navigation, and action button flows with consequence assertions.

  Background:
    Given User is on the Stewie AI home page

  @smoke
  Scenario: User searches for assets with a real query
    When User enters a valid asset search query
    And User submits the asset search
    Then The My Domain Health section is updated with search results

  @regression @validation
  Scenario: User submits asset search with empty query
    When User submits the asset search with no input
    Then A validation message is displayed for the search box

  @smoke
  Scenario: User interacts with Ask Stewie chat
    When User types a question in the Ask Stewie chat box
    And User clicks the Ask button
    Then The Ask Stewie chat panel displays the response

  @smoke
  Scenario: User navigates using the sidebar
    When User clicks the Data Catalog navigation button
    Then The Stewie Terminal heading is visible

  @smoke
  Scenario: User triggers Admin Actions via action button
    When User clicks the Data Quality action button
    Then The Admin Actions section is displayed
