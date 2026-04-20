Feature: Source Connection navigation and action coverage
  Tests navigation and action buttons on the Source Connection page

  Background:
    Given User is on the Source Connection page

  @smoke
  Scenario: Navigate to Home and verify page heading
    When User clicks the Home button
    Then The Source Connection heading is visible

  @smoke
  Scenario: Open sidebar and verify sidebar closes
    When User clicks the Close sidebar button
    Then The sidebar is no longer visible

  @regression @navigation
  Scenario: Navigate to Oracle Database and verify Connect a Data Source heading
    When User clicks the Oracle Database button
    Then The Connect a Data Source heading is visible

  @smoke
  Scenario: Click Ask Stewie and verify Stewie AI interface appears
    When User clicks the Ask Stewie button
    Then The Stewie AI interface is displayed

  @smoke
  Scenario: Click Data Catalog and verify catalog section appears
    When User clicks the Data Catalog button
    Then The catalog section is visible
