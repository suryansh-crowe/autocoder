Feature: Stewie AI Agent Page Core Interactions
  Tests main search, navigation, and action flows for the Agents page.

  Background:
    Given User is on the Stewie AI Agents page

  @smoke
  Scenario: User searches for an agent by name
    When User enters a valid agent name in the search box
    Then The Data Quality Agent heading is displayed

  @regression @validation
  Scenario: User submits an empty search query
    When User submits the search box with no input
    Then The Agents heading remains visible

  @smoke
  Scenario: User navigates to the Home page
    When User clicks the Home button
    Then The Home page heading is displayed

  @smoke
  Scenario: User closes the sidebar
    When User clicks the Close sidebar button
    Then The sidebar is no longer visible

  @smoke
  Scenario: User opens the Ask Stewie panel
    When User clicks the Ask Stewie button
    Then The Ask Stewie message box is visible

  @smoke
  Scenario: User opens the Data Catalog section
    When User clicks the Data Catalog button
    Then The Data Catalog section heading is displayed
