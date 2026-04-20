Feature: Stewie AI Sources Page Navigation and Actions
  Test navigation and action buttons on the Stewie AI Sources page with consequence assertions.

  Background:
    Given User is on the Stewie AI Sources page

  @regression @navigation
  Scenario: Navigate to Home and verify Recent Pipelines heading appears
    When User clicks the Home button
    Then The Recent Pipelines heading is visible

  @smoke
  Scenario: Click Ask Stewie and verify chat panel is displayed
    When User clicks the Ask Stewie button
    Then The Stewie chat panel is displayed

  @smoke
  Scenario: Click Data Catalog and verify Pipeline Status heading appears
    When User clicks the Data Catalog button
    Then The Pipeline Status heading is visible

  @smoke
  Scenario: Click Source Connection and verify Execution Logs heading appears
    When User clicks the Source Connection button
    Then The Execution Logs heading is visible

  @smoke
  Scenario: Click Notifications and verify notification panel is displayed
    When User clicks the Notifications button
    Then The notification panel is displayed
